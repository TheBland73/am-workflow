"""会议、参会者、发言、决策点的读写。

全部是同步的短事务（单连接池红线：会话块内不 await）。函数之间只传 id 和字典，
不把 ORM 对象带出会话。发言的序号在同一个事务里取"最大值加一"。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func

from app.db.base import session_scope
from app.db.models import (MEETING_ACTIVE, DecisionPoint, Meeting, MeetingIntervention,
                           MeetingParticipant, MeetingTurn)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require(s, model, row_id: int, what: str):
    """取一行，不存在就报错。

    静默跳过会让状态存不进去却没有任何声音，后面的步骤会按"已保存"继续跑。
    """
    row = s.get(model, row_id)
    if row is None:
        raise LookupError(f"{what} {row_id} 不存在")
    return row


# ---------------- 会议 ----------------
def _meeting_dict(row: Meeting) -> dict:
    return {
        "id": row.id, "run_id": row.run_id, "node_id": row.node_id, "node_key": row.node_key,
        "protocol": row.protocol, "status": row.status, "phase": row.phase, "round": row.round,
        "charter": dict(row.charter_json or {}), "config": dict(row.config_json or {}),
        "state": dict(row.state_json or {}), "created_at": row.created_at,
        "finished_at": row.finished_at,
    }


def create_meeting(*, run_id: str, node_id: int, node_key: str, protocol: str,
                   charter: dict, config: dict) -> int:
    with session_scope() as s:
        row = Meeting(run_id=run_id, node_id=node_id, node_key=node_key, protocol=protocol,
                      status=MEETING_ACTIVE, phase="", charter_json=charter or {},
                      config_json=config or {})
        s.add(row)
        s.flush()
        return row.id


def latest_meeting_for_node(node_id: int) -> dict | None:
    with session_scope() as s:
        row = s.query(Meeting).filter(Meeting.node_id == node_id).order_by(Meeting.id.desc()).first()
        return _meeting_dict(row) if row else None


def latest_meeting(run_id: str, protocol: str) -> dict | None:
    """一个运行里最近的一场某类会议（例如评审会议要读讨论会议的名单与账本）。"""
    with session_scope() as s:
        row = (
            s.query(Meeting)
            .filter(Meeting.run_id == run_id, Meeting.protocol == protocol)
            .order_by(Meeting.id.desc())
            .first()
        )
        return _meeting_dict(row) if row else None


def get_meeting(meeting_id: int) -> dict:
    with session_scope() as s:
        return _meeting_dict(_require(s, Meeting, meeting_id, "会议"))


def update_meeting(meeting_id: int, **fields) -> None:
    with session_scope() as s:
        row = _require(s, Meeting, meeting_id, "会议")
        if fields.get("finished_at") is True:
            fields = {**fields, "finished_at": _now()}
        for key, value in fields.items():
            setattr(row, key, value)


def get_state(meeting_id: int) -> dict:
    return get_meeting(meeting_id)["state"]


def set_state(meeting_id: int, state: dict) -> None:
    update_meeting(meeting_id, state_json=dict(state))


# ---------------- 参会者 ----------------
def _participant_dict(row: MeetingParticipant) -> dict:
    return {
        "id": row.id, "meeting_id": row.meeting_id, "role_key": row.role_key,
        "display_name": row.display_name, "mode": row.mode, "system_prompt": row.system_prompt,
        "model_id": row.model_id, "source": row.source, "expertise": list(row.expertise_json or []),
        "status": row.status, "position": row.position,
    }


def add_participant(meeting_id: int, *, role_key: str | None, display_name: str, mode: str,
                    system_prompt: str = "", model_id: int | None = None,
                    source: str = "template", expertise: list | None = None) -> int:
    with session_scope() as s:
        last = (
            s.query(func.max(MeetingParticipant.position))
            .filter(MeetingParticipant.meeting_id == meeting_id)
            .scalar() or 0
        )
        row = MeetingParticipant(
            meeting_id=meeting_id, role_key=role_key, display_name=display_name, mode=mode,
            system_prompt=system_prompt, model_id=model_id, source=source,
            expertise_json=expertise or [], position=last + 1,
        )
        s.add(row)
        s.flush()
        return row.id


def list_participants(meeting_id: int, active_only: bool = True) -> list[dict]:
    with session_scope() as s:
        query = s.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id)
        if active_only:
            query = query.filter(MeetingParticipant.status == "active")
        return [_participant_dict(r) for r in query.order_by(MeetingParticipant.position.asc()).all()]


def replace_participants(meeting_id: int, participants: list[dict],
                         editable_modes: tuple[str, ...]) -> None:
    """把可编辑模式的参会者整体换成 participants，同一个事务里完成。

    主持人、需求方代理、主席等不可编辑的模式原样保留；被替换的行标为 removed 供审计。
    任何一条写入失败都整体回滚，不会留下半套名单。
    """
    with session_scope() as s:
        rows = (
            s.query(MeetingParticipant)
            .filter(MeetingParticipant.meeting_id == meeting_id)
            .order_by(MeetingParticipant.position.asc())
            .all()
        )
        position = max((row.position for row in rows), default=0)
        for row in rows:
            if row.status == "active" and row.mode in editable_modes:
                row.status = "removed"
        for item in participants:
            position += 1
            s.add(MeetingParticipant(
                meeting_id=meeting_id, role_key=item.get("role_key"),
                display_name=item["display_name"], mode=item["mode"],
                system_prompt=item.get("system_prompt") or "",
                model_id=item.get("model_id"), source="user",
                expertise_json=[], position=position))


def update_participant(participant_id: int, **fields) -> None:
    with session_scope() as s:
        row = _require(s, MeetingParticipant, participant_id, "参会者")
        for key, value in fields.items():
            setattr(row, key, value)


# ---------------- 发言 ----------------
def add_turn(meeting_id: int, *, round: int, phase: str, speaker_id: int | None,
             speaker_name: str, kind: str, decision_point_id: int | None = None,
             content: str = "", reply_to: int | None = None) -> int:
    with session_scope() as s:
        last = (
            s.query(func.max(MeetingTurn.seq))
            .filter(MeetingTurn.meeting_id == meeting_id)
            .scalar() or 0
        )
        row = MeetingTurn(
            meeting_id=meeting_id, seq=last + 1, round=round, phase=phase, speaker_id=speaker_id,
            speaker_name=speaker_name, kind=kind, decision_point_id=decision_point_id,
            content=content, reply_to=reply_to,
        )
        s.add(row)
        s.flush()
        return row.id


def finish_turn(turn_id: int, *, content: str, structured: dict | None = None,
                tokens_in: int = 0, tokens_out: int = 0, cached_tokens: int = 0,
                model_id: int | None = None) -> None:
    with session_scope() as s:
        row = _require(s, MeetingTurn, turn_id, "发言")
        row.content = content
        row.structured_json = structured or {}
        row.tokens_in = tokens_in
        row.tokens_out = tokens_out
        row.cached_tokens = cached_tokens
        row.model_id = model_id


def _turn_dict(row: MeetingTurn) -> dict:
    return {"id": row.id, "seq": row.seq, "round": row.round, "phase": row.phase,
            "speaker_id": row.speaker_id, "speaker_name": row.speaker_name, "kind": row.kind,
            "decision_point_id": row.decision_point_id, "content": row.content,
            "structured": dict(row.structured_json or {}), "reply_to": row.reply_to,
            "tokens_in": row.tokens_in, "tokens_out": row.tokens_out,
            "cached_tokens": row.cached_tokens}


def list_turns(meeting_id: int, since_seq: int = 0, limit: int = 500) -> list[dict]:
    with session_scope() as s:
        rows = (
            s.query(MeetingTurn)
            .filter(MeetingTurn.meeting_id == meeting_id, MeetingTurn.seq > since_seq)
            .order_by(MeetingTurn.seq.asc())
            .limit(limit)
            .all()
        )
        return [_turn_dict(r) for r in rows]


def completed_speakers(meeting_id: int, *, phase: str, round: int, kind: str) -> set[int]:
    """某步骤某轮里，已经有完整发言的参会者。中断后重入时据此跳过，不重复调用。

    发言行在调用模型之前就建好了，内容在调用结束后才填上；进程被杀时会留下内容为空的行，
    那不算说完。"""
    with session_scope() as s:
        rows = (
            s.query(MeetingTurn.speaker_id)
            .filter(MeetingTurn.meeting_id == meeting_id, MeetingTurn.phase == phase,
                    MeetingTurn.round == round, MeetingTurn.kind == kind,
                    MeetingTurn.speaker_id.isnot(None), MeetingTurn.content != "")
            .all()
        )
        return {row[0] for row in rows}


# ---------------- 决策点 ----------------
def _point_dict(row: DecisionPoint) -> dict:
    return {
        "id": row.id, "meeting_id": row.meeting_id, "index": row.index, "title": row.title,
        "question": row.question, "options": list(row.options_json or []),
        "raised_by": row.raised_by, "status": row.status,
        "conclusion": row.conclusion, "rationale": row.rationale,
        "exchange_summary": row.exchange_summary, "stances": dict(row.stances_json or {}),
        "origin_round": row.origin_round, "closed_round": row.closed_round,
        "superseded_by": row.superseded_by,
    }


def upsert_decision_point(meeting_id: int, index: str, **fields) -> int:
    with session_scope() as s:
        row = (
            s.query(DecisionPoint)
            .filter(DecisionPoint.meeting_id == meeting_id, DecisionPoint.index == index)
            .first()
        )
        if row is None:
            row = DecisionPoint(meeting_id=meeting_id, index=index)
            s.add(row)
        for key, value in fields.items():
            setattr(row, key, value)
        s.flush()
        return row.id


def list_decision_points(meeting_id: int) -> list[dict]:
    with session_scope() as s:
        rows = (
            s.query(DecisionPoint)
            .filter(DecisionPoint.meeting_id == meeting_id)
            .order_by(DecisionPoint.id.asc())
            .all()
        )
        return [_point_dict(r) for r in rows]


def update_decision_point(point_id: int, **fields) -> None:
    with session_scope() as s:
        row = _require(s, DecisionPoint, point_id, "决策点")
        for key, value in fields.items():
            setattr(row, key, value)


# ---------------- 插话 ----------------
def queue_intervention(meeting_id: int, *, content: str, created_by: str = "user") -> int:
    with session_scope() as s:
        row = MeetingIntervention(meeting_id=meeting_id, content=content, status="queued",
                                  created_by=created_by)
        s.add(row)
        s.flush()
        return row.id


def take_queued_interventions(meeting_id: int) -> list[dict]:
    """排队中的插话，按提交顺序。

    这里不改状态：等插话写进纪要，才由 attach_intervention_turn 标为已注入。
    取出与写入之间如果中断，插话还在队列里，下次会被重新取出，不会丢。
    """
    with session_scope() as s:
        rows = (
            s.query(MeetingIntervention)
            .filter(MeetingIntervention.meeting_id == meeting_id,
                    MeetingIntervention.status == "queued")
            .order_by(MeetingIntervention.id.asc())
            .all()
        )
        return [{"id": r.id, "content": r.content, "created_by": r.created_by} for r in rows]


def attach_intervention_turn(intervention_id: int, turn_id: int) -> None:
    """记下插话被注入成了哪一条发言；从这一刻起它才离开队列。"""
    with session_scope() as s:
        row = _require(s, MeetingIntervention, intervention_id, "插话")
        row.status = "injected"
        row.injected_turn_id = turn_id


def count_turns(meeting_id: int) -> int:
    with session_scope() as s:
        return s.query(func.count(MeetingTurn.id)).filter(
            MeetingTurn.meeting_id == meeting_id).scalar() or 0


def get_turn(meeting_id: int, turn_id: int) -> dict | None:
    """按编号取一条有内容的发言；步骤用状态里记下的编号找回自己那一步的发言。"""
    with session_scope() as s:
        row = (
            s.query(MeetingTurn)
            .filter(MeetingTurn.meeting_id == meeting_id, MeetingTurn.id == turn_id,
                    MeetingTurn.content != "")
            .first()
        )
        if row is None:
            return None
        return {"turn_id": row.id, "content": row.content,
                "data": dict(row.structured_json) if row.structured_json else None}


def find_turn(meeting_id: int, *, phase: str, round: int, kind: str,
              speaker_id: int) -> dict | None:
    """取某步骤某轮里某参会者的那条发言，断点续跑时用。"""
    with session_scope() as s:
        row = (
            s.query(MeetingTurn)
            .filter(MeetingTurn.meeting_id == meeting_id, MeetingTurn.phase == phase,
                    MeetingTurn.round == round, MeetingTurn.kind == kind,
                    MeetingTurn.speaker_id == speaker_id, MeetingTurn.content != "")
            .order_by(MeetingTurn.seq.desc())
            .first()
        )
        if row is None:
            return None
        return {"turn_id": row.id, "content": row.content,
                "data": dict(row.structured_json) if row.structured_json else None}


def round_turns(meeting_id: int, *, round: int, kinds: list[str] | None = None) -> list[dict]:
    """某一轮里有内容的发言，按序号升序。kinds 给出时只取这些类型。

    进程被杀时留下的空白发言行不算数。
    """
    with session_scope() as s:
        query = s.query(MeetingTurn).filter(
            MeetingTurn.meeting_id == meeting_id, MeetingTurn.round == round,
            MeetingTurn.content != "")
        if kinds:
            query = query.filter(MeetingTurn.kind.in_(kinds))
        rows = query.order_by(MeetingTurn.seq.asc()).all()
        return [_turn_dict(r) for r in rows]


def turns_upto(meeting_id: int, *, upto_round: int,
               kinds: list[str] | None = None) -> list[dict]:
    """截至某一轮（含）的发言，按序号升序。用一条查询取跨多轮的内容。

    kinds 给出时只取这些类型；不给时取全部类型（对照实验的全量历史视图）。
    """
    with session_scope() as s:
        query = s.query(MeetingTurn).filter(
            MeetingTurn.meeting_id == meeting_id, MeetingTurn.round <= upto_round,
            MeetingTurn.content != "")
        if kinds:
            query = query.filter(MeetingTurn.kind.in_(kinds))
        rows = query.order_by(MeetingTurn.seq.asc()).all()
        return [_turn_dict(r) for r in rows]


def set_stance(point_id: int, participant_id: int, stance: dict) -> None:
    """写入某个参会者在某个决策点上的最新表态；上一轮的等级与理由留进 history。"""
    with session_scope() as s:
        row = s.get(DecisionPoint, point_id)
        if row is None:
            raise LookupError(f"决策点不存在：{point_id}")
        stances = dict(row.stances_json or {})
        previous = dict(stances.get(str(participant_id)) or {})
        history = list(previous.get("history") or [])
        if previous.get("level") is not None and previous.get("round") != stance.get("round"):
            history.append({"round": previous.get("round"), "level": previous.get("level"),
                            "reason": previous.get("reason")})
        stances[str(participant_id)] = {**stance, "history": history}
        row.stances_json = stances
