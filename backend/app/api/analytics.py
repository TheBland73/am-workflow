"""全局运行统计（Analytics）：只读聚合。

口径（六个接口共用同一个 `_window()`，保证卡片、图表、列表范围一致）：

- **时间基准**：`runs.created_at`（UTC）。`days=7/30/90` 表示「含今天在内的最近 N 个
  自然日」，按 UTC 日历切桶；也可用 `start`/`end`（ISO 日期或时间）指定自定义区间。
- **成功**：`status == "finished"`；**失败**：`status == "failed"`（`stopped` 是人工停止，
  不算失败）。成功率 = 成功 / 总数，总数为 0 时返回 0.0，不做除法。
- **耗时**：只统计 `finished` 且 `finished_at` 齐全的运行，失败运行不计入，避免把
  「失败前已跑的时间」混进成功耗时。
- **Token**：`node_attempts.tokens_in / tokens_out`，按 `nodes.run_id` 归集。
- **成本**：复用 `GET /runs/{id}/metrics` 的同一套算法——`price_in/price_out` 按每百万
  token 折算，仅对 `node_attempts.model_id` 有记录且模型配了单价的 attempt 计费。
  没有可用单价时 `available=false`，前端显示「暂无成本数据」，**不按 token 估算**。

本模块只读：不改表结构、不写任何行、不触碰编排逻辑。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from sqlalchemy import func

from app.db.base import session_scope
from app.db.models import ModelEntry, Node, NodeAttempt, ProviderConfig, Run, Task

router = APIRouter(tags=["analytics"])

ALLOWED_DAYS = (7, 30, 90)
MAX_SPAN_DAYS = 366
MAX_LIMIT = 100
DEFAULT_LIMIT = 10

# 与前端 types.ts 的 RunStatus 一一对应；顺序即状态分布图的展示顺序。
RUN_STATUSES = (
    "created", "running", "waiting_human", "finished", "failed", "stopped",
)
SUCCESS_STATUS = "finished"
FAILED_STATUS = "failed"

COST_NOTE = "按 model_entries.price_in / price_out（每百万 token）估算，仅统计已记录模型的 attempt"
COST_MISSING_NOTE = "尚未配置模型单价，暂无成本数据"


# --------------------------------------------------------------------------- #
# 时间窗
# --------------------------------------------------------------------------- #
def _parse_dt(raw: str, *, name: str) -> tuple[datetime, bool]:
    """解析 ISO 日期或时间；返回 (naive UTC, 是否只给了日期)。

    SQLite 里 `DateTime(timezone=True)` 实际按 naive UTC 字符串存（`utcnow()` 写的是
    UTC），所以边界统一换成 naive UTC，避免 aware/naive 混比。
    """
    text = (raw or "").strip()
    if not text:
        raise HTTPException(400, f"{name} 不能为空")
    date_only = len(text) == 10
    try:
        if date_only:
            value = datetime.strptime(text, "%Y-%m-%d")
        else:
            value = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"{name} 不是合法的 ISO 日期或时间：{raw}") from None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value, date_only


def _window(days: int, start: str | None, end: str | None) -> dict:
    """解析统计时间窗。非法参数一律 400，不做静默纠正。"""
    if not isinstance(days, int) or isinstance(days, bool) or days not in ALLOWED_DAYS:
        raise HTTPException(400, f"days 只支持 {list(ALLOWED_DAYS)}，收到 {days!r}")
    if (start is None) != (end is None):
        raise HTTPException(400, "start 与 end 必须同时提供")

    if start is not None and end is not None:
        begin, _ = _parse_dt(start, name="start")
        finish, finish_is_date = _parse_dt(end, name="end")
        end_exclusive = finish + timedelta(days=1) if finish_is_date else finish
        if end_exclusive <= begin:
            raise HTTPException(400, "时间范围不合法：end 必须晚于 start")
    else:
        today = datetime.now(timezone.utc).replace(tzinfo=None).date()
        begin = datetime.combine(today - timedelta(days=days - 1), datetime.min.time())
        end_exclusive = datetime.combine(today + timedelta(days=1), datetime.min.time())

    last_day = (end_exclusive - timedelta(microseconds=1)).date()
    span = (last_day - begin.date()).days + 1
    if span > MAX_SPAN_DAYS:
        raise HTTPException(400, f"时间范围过大，最多 {MAX_SPAN_DAYS} 天（当前 {span} 天）")
    return {
        "days": span,
        "start": begin,
        "end": end_exclusive,
        "start_date": begin.date(),
        "end_date": last_day,
        "payload": {
            "days": span,
            "start": f"{begin.isoformat()}Z",
            "end": f"{end_exclusive.isoformat()}Z",
            "start_date": begin.date().isoformat(),
            "end_date": last_day.isoformat(),
            "basis": "created_at",
            "timezone": "UTC",
        },
    }


def _limit(value: int, *, default: int = DEFAULT_LIMIT) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise HTTPException(400, "limit 必须是整数")
    if value < 1 or value > MAX_LIMIT:
        raise HTTPException(400, f"limit 取值范围 1..{MAX_LIMIT}，收到 {value}")
    return value


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


# --------------------------------------------------------------------------- #
# 聚合（固定查询数，不做 per-run 查询）
# --------------------------------------------------------------------------- #
def _status_counts(s, window: dict) -> dict[str, int]:
    rows = (
        s.query(Run.status, func.count(Run.id))
        .filter(Run.created_at >= window["start"], Run.created_at < window["end"])
        .group_by(Run.status)
        .all()
    )
    counts = {status: 0 for status in RUN_STATUSES}
    for status, count in rows:
        counts[str(status)] = counts.get(str(status), 0) + int(count)
    return counts


def _durations(s, window: dict) -> dict:
    """已完成运行的墙钟耗时（毫秒）。失败/停止不计入，避免污染成功率口径。"""
    rows = (
        s.query(Run.created_at, Run.finished_at)
        .filter(
            Run.status == SUCCESS_STATUS,
            Run.finished_at.isnot(None),
            Run.created_at >= window["start"],
            Run.created_at < window["end"],
        )
        .all()
    )
    spans = [ms for ms in (_ms(a, b) for a, b in rows) if ms is not None]
    if not spans:
        return {"avg_ms": None, "min_ms": None, "max_ms": None, "total_ms": 0,
                "samples": 0, "basis": SUCCESS_STATUS}
    return {
        "avg_ms": int(sum(spans) / len(spans)),
        "min_ms": min(spans),
        "max_ms": max(spans),
        "total_ms": sum(spans),
        "samples": len(spans),
        "basis": SUCCESS_STATUS,
    }


def _tokens_by_run(
    s, window: dict | None = None, run_ids: list[str] | None = None,
) -> dict[str, tuple[int, int]]:
    q = (
        s.query(
            Node.run_id,
            func.coalesce(func.sum(NodeAttempt.tokens_in), 0),
            func.coalesce(func.sum(NodeAttempt.tokens_out), 0),
        )
        .join(NodeAttempt, NodeAttempt.node_id == Node.id)
        .join(Run, Run.id == Node.run_id)
    )
    if run_ids is not None:
        if not run_ids:
            return {}
        q = q.filter(Node.run_id.in_(run_ids))
    elif window is not None:
        q = q.filter(Run.created_at >= window["start"], Run.created_at < window["end"])
    else:
        raise ValueError("_tokens_by_run 需要 window 或 run_ids")
    return {
        str(run_id): (int(tin or 0), int(tout or 0))
        for run_id, tin, tout in q.group_by(Node.run_id).all()
    }


def _prices(s) -> dict[int, dict]:
    rows = (
        s.query(
            ModelEntry.id, ModelEntry.model_name, ModelEntry.price_in, ModelEntry.price_out,
            ProviderConfig.name,
        )
        .outerjoin(ProviderConfig, ProviderConfig.id == ModelEntry.provider_id)
        .all()
    )
    return {
        int(row[0]): {
            "model_name": row[1] or "",
            "provider": row[4] or "",
            "price_in": float(row[2] or 0.0),
            "price_out": float(row[3] or 0.0),
        }
        for row in rows
    }


def _attempt_rows(s, window: dict | None = None, run_ids: list[str] | None = None):
    """(model_id, run_id, tokens_in, tokens_out) 的原始分组行，供成本/模型统计复用。"""
    q = (
        s.query(
            NodeAttempt.model_id,
            Node.run_id,
            func.coalesce(func.sum(NodeAttempt.tokens_in), 0),
            func.coalesce(func.sum(NodeAttempt.tokens_out), 0),
            func.count(NodeAttempt.id),
        )
        .join(Node, Node.id == NodeAttempt.node_id)
        .join(Run, Run.id == Node.run_id)
    )
    if run_ids is not None:
        if not run_ids:
            return []
        q = q.filter(Node.run_id.in_(run_ids))
    elif window is not None:
        q = q.filter(Run.created_at >= window["start"], Run.created_at < window["end"])
    else:
        raise ValueError("_attempt_rows 需要 window 或 run_ids")
    return q.group_by(NodeAttempt.model_id, Node.run_id).all()


def _cost(in_tokens: int, out_tokens: int, price: dict | None) -> float:
    if not price:
        return 0.0
    return in_tokens / 1_000_000 * price["price_in"] + out_tokens / 1_000_000 * price["price_out"]


def _priced_model_ids(rows, prices: dict[int, dict]) -> set[int]:
    """本统计范围内真正成交过、且配了非零单价的模型集合。

    `available` 按「当前范围」判定而不是「库里存在过付费模型」：某个窗口里如果只有
    未配单价的模型，成本就应当显示为空，而不是给出一个 0 元结论。
    """
    ids: set[int] = set()
    for model_id, *_rest in rows:
        if model_id is None:
            continue
        price = prices.get(int(model_id))
        if price and (price["price_in"] or price["price_out"]):
            ids.add(int(model_id))
    return ids


def _cost_meta(priced_models: int) -> dict:
    return {
        "available": priced_models > 0,
        "currency": None,
        "priced_models": priced_models,
        "note": COST_NOTE if priced_models else COST_MISSING_NOTE,
    }


def _node_counts(s, run_ids: list[str]) -> dict[str, dict[str, int]]:
    if not run_ids:
        return {}
    rows = (
        s.query(Node.run_id, Node.status, func.count(Node.id))
        .filter(Node.run_id.in_(run_ids))
        .group_by(Node.run_id, Node.status)
        .all()
    )
    out: dict[str, dict[str, int]] = {}
    for run_id, status, count in rows:
        item = out.setdefault(str(run_id), {"total": 0, "done": 0})
        item["total"] += int(count)
        if status in ("done", "skipped"):
            item["done"] += int(count)
    return out


def _task_counts(s, run_ids: list[str]) -> dict[str, dict[str, int]]:
    if not run_ids:
        return {}
    rows = (
        s.query(Task.run_id, Task.status, func.count(Task.id))
        .filter(Task.run_id.in_(run_ids))
        .group_by(Task.run_id, Task.status)
        .all()
    )
    out: dict[str, dict[str, int]] = {}
    for run_id, status, count in rows:
        item = out.setdefault(str(run_id), {"total": 0, "done": 0})
        item["total"] += int(count)
        if status == "done":
            item["done"] += int(count)
    return out


def _run_rows(s, window: dict, status: str | None = None) -> list[Run]:
    """按窗口取运行行；`recent` / `failed` 需要额外排序与 limit，故各自查询。"""
    q = s.query(Run).filter(Run.created_at >= window["start"], Run.created_at < window["end"])
    if status:
        q = q.filter(Run.status == status)
    return q.order_by(Run.created_at.desc()).all()


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
@router.get("/analytics/overview")
def analytics_overview(days: int = 7, start: str | None = None, end: str | None = None) -> dict:
    """统计卡片：运行数与状态、成功率、平均耗时、Token 与成本。"""
    window = _window(days, start, end)
    with session_scope() as s:
        counts = _status_counts(s, window)
        total = sum(counts.values())
        durations = _durations(s, window)
        tokens = {"input": 0, "output": 0}
        for tin, tout in _tokens_by_run(s, window).values():
            tokens["input"] += tin
            tokens["output"] += tout
        prices = _prices(s)
        rows = _attempt_rows(s, window)
        cost_total = 0.0
        unpriced_tokens = 0
        for model_id, _run_id, tin, tout, _attempts in rows:
            price = prices.get(int(model_id)) if model_id is not None else None
            if price is None or not (price["price_in"] or price["price_out"]):
                unpriced_tokens += int(tin) + int(tout)
                continue
            cost_total += _cost(int(tin), int(tout), price)
        cost = _cost_meta(len(_priced_model_ids(rows, prices)))

    return {
        "window": window["payload"],
        "runs": {
            "total": total,
            "finished": counts.get(SUCCESS_STATUS, 0),
            "failed": counts.get(FAILED_STATUS, 0),
            "running": counts.get("running", 0),
            "waiting_human": counts.get("waiting_human", 0),
            "stopped": counts.get("stopped", 0),
            "created": counts.get("created", 0),
        },
        "success_rate": round(counts.get(SUCCESS_STATUS, 0) / total, 4) if total else 0.0,
        "duration": durations,
        "tokens": {
            "input": tokens["input"],
            "output": tokens["output"],
            "total": tokens["input"] + tokens["output"],
        },
        "cost": {**cost, "estimated_cost": round(cost_total, 8), "unpriced_tokens": unpriced_tokens},
    }


@router.get("/analytics/trends")
def analytics_trends(days: int = 7, start: str | None = None, end: str | None = None) -> dict:
    """按天趋势：运行数 / 成功 / 失败 / token；没有记录的日期补 0。"""
    window = _window(days, start, end)
    buckets: dict[date, dict] = {}
    cursor = window["start_date"]
    while cursor <= window["end_date"]:
        buckets[cursor] = {"date": cursor.isoformat(), "runs": 0, "finished": 0, "failed": 0,
                           "tokens_in": 0, "tokens_out": 0}
        cursor += timedelta(days=1)

    with session_scope() as s:
        rows = (
            s.query(Run.id, Run.created_at, Run.status)
            .filter(Run.created_at >= window["start"], Run.created_at < window["end"])
            .order_by(Run.created_at.asc())
            .all()
        )
        tokens = _tokens_by_run(s, window)

    for run_id, created_at, status in rows:
        item = buckets.get(created_at.date())
        if item is None:  # 理论上不会发生；跨窗数据兜底，不丢也不崩
            continue
        item["runs"] += 1
        if status == SUCCESS_STATUS:
            item["finished"] += 1
        elif status == FAILED_STATUS:
            item["failed"] += 1
        tin, tout = tokens.get(str(run_id), (0, 0))
        item["tokens_in"] += tin
        item["tokens_out"] += tout

    items = []
    for bucket in buckets.values():
        items.append({**bucket, "total_tokens": bucket["tokens_in"] + bucket["tokens_out"]})
    return {
        "window": window["payload"],
        "items": items,
        "totals": {
            "runs": sum(i["runs"] for i in items),
            "finished": sum(i["finished"] for i in items),
            "failed": sum(i["failed"] for i in items),
            "tokens_in": sum(i["tokens_in"] for i in items),
            "tokens_out": sum(i["tokens_out"] for i in items),
            "total_tokens": sum(i["total_tokens"] for i in items),
        },
    }


@router.get("/analytics/status-distribution")
def analytics_status_distribution(
    days: int = 7, start: str | None = None, end: str | None = None,
) -> dict:
    """运行状态分布。状态键与数据库一致，前端负责中文标签。"""
    window = _window(days, start, end)
    with session_scope() as s:
        counts = _status_counts(s, window)
    # 只统计 API 白名单内的状态：未知状态不展示，避免前端点进去被 recent-runs 400。
    total = sum(counts.get(status, 0) for status in RUN_STATUSES)
    items = [
        {
            "status": status,
            "count": counts.get(status, 0),
            "ratio": round(counts.get(status, 0) / total, 4) if total else 0.0,
        }
        for status in RUN_STATUSES
    ]
    return {"window": window["payload"], "total": total, "items": items}


@router.get("/analytics/usage")
def analytics_usage(days: int = 7, start: str | None = None, end: str | None = None) -> dict:
    """Token 与成本：总量 + 按模型拆分（模型未知的归入 `model_id: null`）。"""
    window = _window(days, start, end)
    with session_scope() as s:
        prices = _prices(s)
        rows = _attempt_rows(s, window)
        model_attempts = (
            s.query(NodeAttempt.model_id, func.count(NodeAttempt.id))
            .join(Node, Node.id == NodeAttempt.node_id)
            .join(Run, Run.id == Node.run_id)
            .filter(Run.created_at >= window["start"], Run.created_at < window["end"])
            .group_by(NodeAttempt.model_id)
            .all()
        )

    attempts_by_model = {row[0]: int(row[1]) for row in model_attempts}
    by_model: dict[int | None, dict] = {}
    for model_id, _run_id, tin, tout, _count in rows:
        key = int(model_id) if model_id is not None else None
        cost = _cost(int(tin), int(tout), prices.get(key) if key is not None else None)
        item = by_model.setdefault(
            key, {"model_id": key, "model_name": "", "provider": "", "tokens_in": 0,
                  "tokens_out": 0, "total": 0, "attempts": 0, "cost": 0.0},
        )
        item["tokens_in"] += int(tin)
        item["tokens_out"] += int(tout)
        item["total"] += int(tin) + int(tout)
        item["cost"] += cost

    total_tokens = sum(item["total"] for item in by_model.values())
    known = {int(k) for k in by_model if k is not None}
    # 有 attempt 但从未归因到具体模型的：单独保留一个 null 行，避免 token 总数对不上。
    if None in by_model:
        item = by_model[None]
        item["model_name"] = "未记录模型"
        item["attempts"] = attempts_by_model.get(None, 0)
    for model_id in known:
        item = by_model[model_id]
        if model_id in prices:
            item["model_name"] = prices[model_id]["model_name"]
            item["provider"] = prices[model_id]["provider"]
        else:  # 模型已被删除：保留 token 记录，标注名称缺失
            item["model_name"] = f"已删除模型 #{model_id}"
        item["attempts"] = attempts_by_model.get(model_id, 0)

    items = sorted(by_model.values(), key=lambda row: row["total"], reverse=True)
    # 丢掉「0 token 且没有归因模型」的空行（PASS / HUMAN_GATE 这类不调模型的 attempt）。
    items = [
        item for item in items
        if item["total"] > 0 or (item["model_id"] is not None and item["attempts"] > 0)
    ]
    for item in items:
        item["ratio"] = round(item["total"] / total_tokens, 4) if total_tokens else 0.0
        item["cost"] = round(item["cost"], 8)

    tokens_in = sum(item["tokens_in"] for item in items)
    tokens_out = sum(item["tokens_out"] for item in items)
    cost_total = sum(item["cost"] for item in items)
    unpriced = sum(
        item["total"] for item in items
        if item["model_id"] is None
        or item["model_id"] not in prices
        or not (prices[item["model_id"]]["price_in"] or prices[item["model_id"]]["price_out"])
    )
    return {
        "window": window["payload"],
        "tokens": {"input": tokens_in, "output": tokens_out, "total": tokens_in + tokens_out},
        "by_model": items,
        "cost": {
            **_cost_meta(len(_priced_model_ids(rows, prices))),
            "estimated_cost": round(cost_total, 8),
            "unpriced_tokens": unpriced,
            "by_model": [
                {"model_id": i["model_id"], "model_name": i["model_name"],
                 "provider": i["provider"], "cost": i["cost"],
                 "total_tokens": i["total"]}
                for i in items if i["cost"] > 0
            ],
        },
    }


def _run_items(s, runs: list[Run], *, with_nodes: bool = True) -> list[dict]:
    """把 Run 行批量补齐 token / 成本 / 节点与任务进度（全部是 group by，无 N+1）。"""
    if not runs:
        return []
    run_ids = [run.id for run in runs]
    tokens = _tokens_by_run(s, run_ids=run_ids)
    prices = _prices(s)
    attempt_rows = _attempt_rows(s, run_ids=run_ids)
    priced_ids = _priced_model_ids(attempt_rows, prices)
    cost_by_run: dict[str, float] = {}
    priced_runs: set[str] = set()
    for model_id, run_id, tin, tout, _count in attempt_rows:
        key = int(model_id) if model_id is not None else None
        if key in priced_ids:
            priced_runs.add(str(run_id))
        cost_by_run[str(run_id)] = cost_by_run.get(str(run_id), 0.0) + _cost(
            int(tin), int(tout), prices.get(key) if key is not None else None,
        )
    nodes = _node_counts(s, run_ids) if with_nodes else {}
    tasks = _task_counts(s, run_ids) if with_nodes else {}

    items = []
    for run in runs:
        tin, tout = tokens.get(run.id, (0, 0))
        node_stat = nodes.get(run.id, {"total": 0, "done": 0})
        task_stat = tasks.get(run.id, {"total": 0, "done": 0})
        items.append({
            "id": run.id,
            "idea": run.idea_text,
            "status": run.status,
            "current_phase": run.current_phase,
            "created_at": _iso(run.created_at),
            "finished_at": _iso(run.finished_at),
            "duration_ms": _ms(run.created_at, run.finished_at),
            "tokens_in": tin,
            "tokens_out": tout,
            "total_tokens": tin + tout,
            "cost": round(cost_by_run.get(run.id, 0.0), 8),
            "cost_available": str(run.id) in priced_runs,
            "nodes_total": node_stat["total"],
            "nodes_done": node_stat["done"],
            "tasks_total": task_stat["total"],
            "tasks_done": task_stat["done"],
            "error": (run.error or "")[:500] or None,
        })
    return items


@router.get("/analytics/recent-runs")
def analytics_recent_runs(
    days: int = 7, start: str | None = None, end: str | None = None,
    limit: int = DEFAULT_LIMIT, status: str | None = None,
) -> dict:
    """最近运行记录。`status` 可选，用于和状态分布图联动。"""
    window = _window(days, start, end)
    limit = _limit(limit)
    if status and status not in RUN_STATUSES:
        raise HTTPException(400, f"status 只支持 {list(RUN_STATUSES)}，收到 {status!r}")
    with session_scope() as s:
        query = s.query(Run).filter(
            Run.created_at >= window["start"], Run.created_at < window["end"],
        )
        if status:
            query = query.filter(Run.status == status)
        runs = query.order_by(Run.created_at.desc()).limit(limit).all()
        items = _run_items(s, runs)
    return {"window": window["payload"], "limit": limit, "status": status, "items": items}


@router.get("/analytics/failed-runs")
def analytics_failed_runs(
    days: int = 7, start: str | None = None, end: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """失败运行及其失败节点/错误摘要（数据取自 run.error 与 node_attempts.error）。"""
    window = _window(days, start, end)
    limit = _limit(limit)
    with session_scope() as s:
        runs = (
            s.query(Run)
            .filter(
                Run.status == FAILED_STATUS,
                Run.created_at >= window["start"],
                Run.created_at < window["end"],
            )
            .order_by(Run.created_at.desc())
            .limit(limit)
            .all()
        )
        run_ids = [run.id for run in runs]
        failed_nodes: list[Node] = []
        attempts: list[NodeAttempt] = []
        if run_ids:
            failed_nodes = (
                s.query(Node)
                .filter(Node.run_id.in_(run_ids), Node.status == "failed")
                .order_by(Node.order_index.asc(), Node.id.asc())
                .all()
            )
            node_ids = [node.id for node in failed_nodes]
            if node_ids:
                attempts = (
                    s.query(NodeAttempt)
                    .filter(NodeAttempt.node_id.in_(node_ids))
                    .order_by(NodeAttempt.node_id.asc(), NodeAttempt.attempt_no.asc())
                    .all()
                )
        latest: dict[int, NodeAttempt] = {}
        for attempt in attempts:
            latest[attempt.node_id] = attempt
        node_by_run: dict[str, Node] = {}
        for node in failed_nodes:
            node_by_run.setdefault(node.run_id, node)

        items = []
        for run in runs:
            node = node_by_run.get(run.id)
            attempt = latest.get(node.id) if node is not None else None
            node_error = (attempt.error or "") if attempt else ""
            run_error = run.error or ""
            summary = (run_error or node_error or "运行失败，未记录错误信息").strip()
            items.append({
                "id": run.id,
                "idea": run.idea_text,
                "status": run.status,
                "current_phase": run.current_phase,
                "created_at": _iso(run.created_at),
                "finished_at": _iso(run.finished_at),
                "failed_at": _iso(run.finished_at) or _iso(attempt.finished_at if attempt else None),
                "error": summary[:500],
                "error_truncated": len(summary) > 500,
                "node_key": node.node_key if node is not None else None,
                "node_name": node.name if node is not None else None,
                "node_phase": node.phase if node is not None else None,
                "node_error": (node_error[:500] or None),
            })
    return {"window": window["payload"], "limit": limit, "items": items}
