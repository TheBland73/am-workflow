"""全局运行统计（Analytics）API 测试。

约定：本文件所有用例把自己的数据钉在 2031–2032 年的独立月份里，用 `start`/`end`
自定义区间取数，因此不会和真实运行或其它用例的数据互相干扰，断言可以写死具体数字。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db.base import session_scope
from app.db.models import (
    ModelEntry, Node, NodeAttempt, ProviderConfig, Run, Task, Workflow, new_run_id,
)
from app.main import app

_URL = "/api/analytics"

# 每个用例独占一个月份；DAY0 是该月的 1 日 08:00（naive UTC，与 SQLite 存储一致）。
_SEEDED: list[str] = []
_PROVIDERS: list[int] = []
_node_seq = 0


def month(year: int, month_no: int, days: int = 1) -> tuple[datetime, str, str]:
    base = datetime(year, month_no, 1, 8, 0, 0)
    last = f"{year:04d}-{month_no:02d}-{days:02d}"
    return base, f"{year:04d}-{month_no:02d}-01", last


def _workflow_id() -> int:
    with session_scope() as s:
        wf = (
            s.query(Workflow)
            .filter(Workflow.template_key == "analytics_fixture")
            .one_or_none()
        )
        if wf is None:
            wf = Workflow(
                name="analytics fixture", template_key="analytics_fixture", definition_json={},
            )
            s.add(wf)
            s.flush()
        return int(wf.id)


def seed_run(
    *, status: str = "finished", created: datetime, duration_s: float | None = None,
    idea: str = "analytics fixture", error: str | None = None,
) -> str:
    workflow_id = _workflow_id()
    run_id = new_run_id()
    finished = created + timedelta(seconds=duration_s) if duration_s is not None else None
    with session_scope() as s:
        s.add(Run(
            id=run_id, workflow_id=workflow_id, status=status, idea_text=idea,
            created_at=created, finished_at=finished, budget_tokens=1000, error=error,
        ))
    _SEEDED.append(run_id)
    return run_id


def seed_attempt(
    run_id: str, *, tokens_in: int = 0, tokens_out: int = 0, model_id: int | None = None,
    node_status: str = "done", attempt_status: str = "done", node_name: str = "node",
    error: str | None = None, started: datetime | None = None,
    finished: datetime | None = None,
) -> int:
    global _node_seq
    _node_seq += 1
    with session_scope() as s:
        node = Node(
            run_id=run_id, node_key=f"n{_node_seq}", type="AGENT", name=node_name,
            phase="execution", status=node_status, order_index=_node_seq,
        )
        s.add(node)
        s.flush()
        s.add(NodeAttempt(
            node_id=node.id, attempt_no=1, model_id=model_id, status=attempt_status,
            started_at=started or datetime(2000, 1, 1), finished_at=finished,
            tokens_in=tokens_in, tokens_out=tokens_out, error=error,
        ))
        return int(node.id)


def seed_task(run_id: str, *, status: str = "done", uid: str | None = None) -> None:
    with session_scope() as s:
        s.add(Task(run_id=run_id, task_uid=uid or uuid.uuid4().hex[:8], title="task",
                   status=status))


def seed_model(*, price_in: float = 0.0, price_out: float = 0.0, name: str | None = None) -> int:
    label = name or f"m-{uuid.uuid4().hex[:8]}"
    with session_scope() as s:
        provider = ProviderConfig(
            name=f"p-{uuid.uuid4().hex[:8]}", protocol="openai_chat",
            base_url="http://127.0.0.1:9/v1", api_key_enc=None,
        )
        s.add(provider)
        s.flush()
        model = ModelEntry(
            provider_id=provider.id, model_name=label, display_name=label,
            price_in=price_in, price_out=price_out,
        )
        s.add(model)
        s.flush()
        _PROVIDERS.append(int(provider.id))
        return int(model.id)


def retire_seeded() -> None:
    """清理本次用例留下的夹具数据。

    1. 造的 run 全部置 `stopped`：本文件的数据是「未来时间 + 手工插入」的，
       留下 created/running/waiting_human 会被后续用例的 lifespan recover() 当成
       未完成运行去 start()，污染别的用例的断言。
    2. 删掉造的供应商/模型：库里只要存在一条 ModelEntry，后续用例里编排器
       `pool.pick_role_model()` 就不再走「尚未配置可用模型」的快速失败分支，
       运行状态随之改变，同样会影响到别的用例。
    """
    if _SEEDED:
        with session_scope() as s:
            for run in s.query(Run).filter(Run.id.in_(list(_SEEDED))).all():
                if run.status != "stopped":
                    run.status = "stopped"
    if _PROVIDERS:
        with session_scope() as s:
            for provider in s.query(ProviderConfig).filter(
                ProviderConfig.id.in_(list(_PROVIDERS)),
            ).all():
                s.delete(provider)


@pytest.fixture(autouse=True)
def _cleanup() -> None:
    yield
    retire_seeded()
    _SEEDED.clear()
    _PROVIDERS.clear()


# --------------------------------------------------------------------------- #
# 概览卡片
# --------------------------------------------------------------------------- #
def test_overview_empty_window_has_no_zero_division() -> None:
    with TestClient(app) as client:
        body = client.get(f"{_URL}/overview", params={
            "start": "2032-01-01", "end": "2032-01-03",
        }).json()
    assert body["runs"]["total"] == 0
    assert body["success_rate"] == 0.0                      # 0/0 不抛异常
    assert body["duration"]["avg_ms"] is None               # 无样本用 null，不用 0 冒充
    assert body["duration"]["samples"] == 0
    assert body["tokens"] == {"input": 0, "output": 0, "total": 0}
    assert body["cost"]["available"] is False
    assert body["cost"]["estimated_cost"] == 0
    assert body["window"]["days"] == 3
    assert body["window"]["basis"] == "created_at"


def test_overview_counts_and_success_rate() -> None:
    base, start, end = month(2031, 7)
    with TestClient(app) as client:
        seed_run(status="finished", created=base, duration_s=10)
        seed_run(status="failed", created=base + timedelta(minutes=1), error="boom")
        seed_run(status="running", created=base + timedelta(minutes=2))
        seed_run(status="waiting_human", created=base + timedelta(minutes=3))
        body = client.get(f"{_URL}/overview", params={"start": start, "end": end}).json()
    assert body["runs"] == {
        "total": 4, "finished": 1, "failed": 1, "running": 1,
        "waiting_human": 1, "stopped": 0, "created": 0,
    }
    assert body["success_rate"] == 0.25
    assert body["duration"]["samples"] == 1                 # 只有 finished 计入耗时


def test_average_duration_only_counts_finished_runs() -> None:
    base, start, end = month(2031, 8)
    with TestClient(app) as client:
        seed_run(status="finished", created=base, duration_s=10)
        seed_run(status="finished", created=base + timedelta(minutes=1), duration_s=30)
        # 失败运行也记了 finished_at，但不应污染「成功运行平均耗时」
        seed_run(status="failed", created=base + timedelta(minutes=2), duration_s=120, error="x")
        body = client.get(f"{_URL}/overview", params={"start": start, "end": end}).json()
    duration = body["duration"]
    assert duration["samples"] == 2
    assert duration["avg_ms"] == 20_000
    assert duration["min_ms"] == 10_000
    assert duration["max_ms"] == 30_000
    assert duration["total_ms"] == 40_000
    assert duration["basis"] == "finished"


def test_token_totals_are_summed_across_attempts() -> None:
    base, start, end = month(2031, 9)
    with TestClient(app) as client:
        run_id = seed_run(status="finished", created=base, duration_s=5)
        seed_attempt(run_id, tokens_in=100, tokens_out=50)
        seed_attempt(run_id, tokens_in=20, tokens_out=10)
        body = client.get(f"{_URL}/overview", params={"start": start, "end": end}).json()
    assert body["tokens"] == {"input": 120, "output": 60, "total": 180}


def test_cost_uses_configured_model_prices() -> None:
    base, start, end = month(2031, 10)
    model_id = seed_model(price_in=1.0, price_out=2.0)   # 每百万 token
    with TestClient(app) as client:
        run_id = seed_run(status="finished", created=base, duration_s=5)
        seed_attempt(run_id, tokens_in=1_000_000, tokens_out=500_000, model_id=model_id)
        body = client.get(f"{_URL}/overview", params={"start": start, "end": end}).json()
    assert body["cost"]["available"] is True
    assert body["cost"]["priced_models"] == 1
    assert body["cost"]["estimated_cost"] == pytest.approx(2.0)
    assert body["cost"]["unpriced_tokens"] == 0


def test_cost_is_empty_when_no_price_is_configured() -> None:
    base, start, end = month(2031, 11)
    model_id = seed_model(price_in=0.0, price_out=0.0)
    with TestClient(app) as client:
        run_id = seed_run(status="finished", created=base, duration_s=5)
        seed_attempt(run_id, tokens_in=1000, tokens_out=500, model_id=model_id)
        body = client.get(f"{_URL}/overview", params={"start": start, "end": end}).json()
    assert body["cost"]["available"] is False            # 不拿 token 乱估费用
    assert body["cost"]["estimated_cost"] == 0
    assert body["cost"]["unpriced_tokens"] == 1500
    assert body["tokens"]["total"] == 1500


# --------------------------------------------------------------------------- #
# 趋势与状态分布
# --------------------------------------------------------------------------- #
def test_trends_fill_days_without_runs() -> None:
    base, start, end = month(2031, 12, days=5)          # 12-01 .. 12-05 共 5 天
    with TestClient(app) as client:
        a = seed_run(status="finished", created=base, duration_s=1)
        seed_attempt(a, tokens_in=10, tokens_out=5)
        seed_run(status="failed", created=base + timedelta(hours=1), error="boom")
        seed_run(status="running", created=base + timedelta(days=2, hours=2))
        body = client.get(f"{_URL}/trends", params={"start": start, "end": end}).json()

    items = body["items"]
    assert len(items) == 5
    assert [i["date"] for i in items] == [
        "2031-12-01", "2031-12-02", "2031-12-03", "2031-12-04", "2031-12-05",
    ]
    assert items[0] == {
        "date": "2031-12-01", "runs": 2, "finished": 1, "failed": 1,
        "tokens_in": 10, "tokens_out": 5, "total_tokens": 15,
    }
    assert items[1]["runs"] == 0                        # 没数据的日子补 0，不跳过
    assert items[2]["runs"] == 1
    assert items[2]["finished"] == 0 and items[2]["failed"] == 0
    assert items[3]["runs"] == 0 and items[4]["runs"] == 0
    assert body["totals"]["runs"] == 3
    assert body["totals"]["total_tokens"] == 15


def test_status_distribution_matches_database() -> None:
    base, start, end = month(2032, 2)
    with TestClient(app) as client:
        seed_run(status="finished", created=base, duration_s=1)
        seed_run(status="finished", created=base + timedelta(minutes=1), duration_s=1)
        seed_run(status="failed", created=base + timedelta(minutes=2), error="boom")
        body = client.get(
            f"{_URL}/status-distribution", params={"start": start, "end": end},
        ).json()

    assert body["total"] == 3
    by_status = {item["status"]: item for item in body["items"]}
    assert list(by_status) == [
        "created", "running", "waiting_human", "finished", "failed", "stopped",
    ]
    assert by_status["finished"]["count"] == 2
    assert by_status["finished"]["ratio"] == pytest.approx(0.6667)
    assert by_status["failed"]["count"] == 1
    assert by_status["running"]["count"] == 0
    assert sum(item["count"] for item in body["items"]) == body["total"]


# --------------------------------------------------------------------------- #
# Token / 成本按模型
# --------------------------------------------------------------------------- #
def test_usage_splits_tokens_and_cost_by_model() -> None:
    base, start, end = month(2032, 3)
    priced = seed_model(price_in=2.0, price_out=4.0, name="priced-model")
    free = seed_model(price_in=0.0, price_out=0.0, name="free-model")
    with TestClient(app) as client:
        run_id = seed_run(status="finished", created=base, duration_s=1)
        seed_attempt(run_id, tokens_in=1_000_000, tokens_out=0, model_id=priced)
        seed_attempt(run_id, tokens_in=100, tokens_out=100, model_id=free)
        seed_attempt(run_id, tokens_in=10, tokens_out=10, model_id=None)
        body = client.get(f"{_URL}/usage", params={"start": start, "end": end}).json()

    assert body["tokens"]["total"] == 1_000_220
    by_model = {item["model_name"]: item for item in body["by_model"]}
    assert by_model["priced-model"]["total"] == 1_000_000
    assert by_model["priced-model"]["cost"] == pytest.approx(2.0)
    assert by_model["priced-model"]["provider"].startswith("p-")
    assert by_model["free-model"]["cost"] == 0
    assert by_model["未记录模型"]["total"] == 20
    # 按 token 降序，且比例相加为 1
    assert body["by_model"][0]["model_name"] == "priced-model"
    assert sum(item["ratio"] for item in body["by_model"]) == pytest.approx(1.0, abs=1e-3)
    assert body["cost"]["available"] is True
    assert [row["model_name"] for row in body["cost"]["by_model"]] == ["priced-model"]


def test_usage_on_empty_window_returns_empty_structure() -> None:
    with TestClient(app) as client:
        body = client.get(f"{_URL}/usage", params={
            "start": "2032-04-01", "end": "2032-04-02",
        }).json()
    assert body["tokens"] == {"input": 0, "output": 0, "total": 0}
    assert body["by_model"] == []
    assert body["cost"]["available"] is False
    assert body["cost"]["estimated_cost"] == 0


# --------------------------------------------------------------------------- #
# 最近运行 / 失败运行
# --------------------------------------------------------------------------- #
def test_recent_runs_order_limit_and_fields() -> None:
    base, start, end = month(2032, 5)
    with TestClient(app) as client:
        older = seed_run(status="finished", created=base, duration_s=30, idea="older run")
        newest = seed_run(
            status="running", created=base + timedelta(hours=2), idea="newest run",
        )
        middle = seed_run(
            status="failed", created=base + timedelta(hours=1), idea="middle run", error="boom",
        )
        seed_attempt(newest, tokens_in=7, tokens_out=3)
        seed_task(older, status="done")
        seed_task(older, status="pending")
        body = client.get(f"{_URL}/recent-runs", params={
            "start": start, "end": end, "limit": 2,
        }).json()
        filtered = client.get(f"{_URL}/recent-runs", params={
            "start": start, "end": end, "status": "failed",
        }).json()
        all_items = client.get(f"{_URL}/recent-runs", params={
            "start": start, "end": end, "limit": 3,
        }).json()["items"]

    assert body["limit"] == 2
    assert [item["id"] for item in body["items"]] == [newest, middle]   # 按创建时间倒序
    assert [item["id"] for item in filtered["items"]] == [middle]
    item = body["items"][0]
    for key in (
        "id", "idea", "status", "current_phase", "created_at", "finished_at", "duration_ms",
        "tokens_in", "tokens_out", "total_tokens", "cost", "cost_available",
        "nodes_total", "nodes_done", "tasks_total", "tasks_done", "error",
    ):
        assert key in item
    assert item["total_tokens"] == 10
    assert item["duration_ms"] is None                     # 未完成，不编造耗时

    older_item = all_items[2]
    assert older_item["id"] == older
    assert older_item["duration_ms"] == 30_000
    assert older_item["tasks_total"] == 2 and older_item["tasks_done"] == 1


def test_recent_runs_without_data_is_empty_list() -> None:
    with TestClient(app) as client:
        body = client.get(f"{_URL}/recent-runs", params={
            "start": "2032-06-01", "end": "2032-06-02",
        }).json()
    assert body["items"] == []


def test_failed_runs_report_error_and_failed_node() -> None:
    base, start, end = month(2032, 7)
    with TestClient(app) as client:
        with_node = seed_run(
            status="failed", created=base, error="编排终止：verify 连续失败",
        )
        node_id = seed_attempt(
            with_node, node_status="failed", attempt_status="failed", node_name="verify",
            error="python -m unittest 退出码 1", finished=base + timedelta(minutes=3),
        )
        without_node = seed_run(
            status="failed", created=base + timedelta(minutes=1), error="预算耗尽",
        )
        seed_run(status="finished", created=base + timedelta(minutes=2), duration_s=1)
        body = client.get(f"{_URL}/failed-runs", params={"start": start, "end": end}).json()

    items = {item["id"]: item for item in body["items"]}
    assert set(items) == {with_node, without_node}          # 只列 failed
    assert items[with_node]["node_name"] == "verify"
    assert items[with_node]["error"].startswith("编排终止")
    assert items[with_node]["node_error"].startswith("python -m unittest")
    assert items[with_node]["failed_at"] is not None
    assert items[without_node]["node_key"] is None
    assert items[without_node]["error"] == "预算耗尽"
    assert node_id > 0


def test_failed_runs_limit_is_honoured() -> None:
    base, start, end = month(2032, 8)
    with TestClient(app) as client:
        for index in range(3):
            seed_run(status="failed", created=base + timedelta(minutes=index), error=f"e{index}")
        body = client.get(f"{_URL}/failed-runs", params={
            "start": start, "end": end, "limit": 2,
        }).json()
    assert len(body["items"]) == 2


# --------------------------------------------------------------------------- #
# 时间筛选与参数校验
# --------------------------------------------------------------------------- #
def test_days_window_is_accepted_and_used() -> None:
    with TestClient(app) as client:
        overview = client.get(f"{_URL}/overview", params={"days": 30}).json()
        trends = client.get(f"{_URL}/trends", params={"days": 30}).json()
        distribution = client.get(f"{_URL}/status-distribution", params={"days": 30}).json()
        wide = client.get(f"{_URL}/status-distribution", params={"days": 90}).json()
    assert overview["window"]["days"] == 30
    assert len(trends["items"]) == 30
    assert trends["items"][0]["date"] <= trends["items"][-1]["date"]
    assert wide["window"]["days"] == 90
    # 同一天数窗口下，「状态分布」总数必须等于「概览」总数（同一口径）
    assert distribution["total"] == overview["runs"]["total"]


def test_time_window_only_counts_runs_inside_it() -> None:
    inside, start, end = month(2032, 9, days=2)
    outside = inside + timedelta(days=10)
    with TestClient(app) as client:
        seed_run(status="finished", created=inside, duration_s=1)
        seed_run(status="finished", created=outside, duration_s=1)
        body = client.get(f"{_URL}/overview", params={"start": start, "end": end}).json()
    assert body["runs"]["total"] == 1
    assert body["window"]["start_date"] == "2032-09-01"
    assert body["window"]["end_date"] == "2032-09-02"


@pytest.mark.parametrize("params", [
    {"days": 13},
    {"days": 0},
    {"days": -7},
    {"start": "2032-10-01"},                        # 只给 start
    {"end": "2032-10-02"},                          # 只给 end
    {"start": "not-a-date", "end": "2032-10-02"},   # 无法解析
    {"start": "2032-10-05", "end": "2032-10-02"},   # 结束早于开始
    {"start": "2000-01-01", "end": "2032-12-31"},   # 超过 366 天
])
def test_invalid_window_is_rejected(params: dict) -> None:
    with TestClient(app) as client:
        for path in ("overview", "trends", "status-distribution", "usage", "recent-runs"):
            resp = client.get(f"{_URL}/{path}", params=params)
            assert resp.status_code == 400, (path, params, resp.text)
            assert isinstance(resp.json()["detail"], str)


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"status": "bogus"}])
def test_invalid_list_params_are_rejected(params: dict) -> None:
    with TestClient(app) as client:
        resp = client.get(f"{_URL}/recent-runs", params={**params, "days": 7})
    assert resp.status_code == 400
    assert isinstance(resp.json()["detail"], str)


def test_response_structure_matches_frontend_contract() -> None:
    with TestClient(app) as client:
        overview = client.get(f"{_URL}/overview", params={"days": 7}).json()
        trends = client.get(f"{_URL}/trends", params={"days": 7}).json()
        usage = client.get(f"{_URL}/usage", params={"days": 7}).json()
        recent = client.get(f"{_URL}/recent-runs", params={"days": 7}).json()
        failed = client.get(f"{_URL}/failed-runs", params={"days": 7}).json()

    assert set(overview) == {"window", "runs", "success_rate", "duration", "tokens", "cost"}
    assert set(overview["runs"]) == {
        "total", "finished", "failed", "running", "waiting_human", "stopped", "created",
    }
    assert set(overview["cost"]) >= {"available", "estimated_cost", "priced_models", "note"}
    assert set(trends) == {"window", "items", "totals"}
    assert set(trends["items"][0]) == {
        "date", "runs", "finished", "failed", "tokens_in", "tokens_out", "total_tokens",
    }
    assert set(usage) == {"window", "tokens", "by_model", "cost"}
    assert set(recent) == {"window", "limit", "status", "items"}
    assert set(failed) == {"window", "limit", "items"}
