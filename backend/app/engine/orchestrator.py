"""编排器：拓扑波次调度、人工门挂起、失败回流、断点恢复（设计规格 §5.2–§5.5）。

编排器是程序，不是 LLM：它只负责推状态、发事件、做判定。
"""
from __future__ import annotations

import asyncio
import traceback
from datetime import datetime, timezone

from app.db.base import session_scope
from app.db.models import Gate, Node, NodeAttempt, Run, Workflow
from app.engine.events import emit
from app.engine.gates import GateError, decide_gate as _decide_gate
from app.engine.runners import NodeContext, NodeFailure, classify_failure, execute_node, lesson_for
from app.engine.templates import phase_names

TERMINAL = {"finished", "failed", "stopped"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Orchestrator:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._running: set[str] = set()
        self._wake: set[str] = set()

    # ---------- 生命周期 ----------
    def start(self, run_id: str) -> None:
        if run_id in self._running:
            self._wake.add(run_id)
            return
        self._running.add(run_id)
        task = asyncio.create_task(self._loop(run_id))
        self._tasks[run_id] = task

        def _cleanup(_t: asyncio.Task) -> None:
            self._running.discard(run_id)
            self._tasks.pop(run_id, None)
            if run_id in self._wake:
                self._wake.discard(run_id)
                self.start(run_id)

        task.add_done_callback(_cleanup)

    def kick(self, run_id: str) -> None:
        """门决策 / 人工编辑之后唤醒调度器。"""
        self.start(run_id)

    async def stop(self, run_id: str) -> None:
        with session_scope() as s:
            run = s.get(Run, run_id)
            if run is None:
                return
            run.status = "stopped"
        await emit(run_id, "run_stopped", None, {"reason": "用户停止"})
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()

    async def shutdown(self) -> None:
        """进程退出：取消所有调度协程（不改变 run 状态）。"""
        for task in list(self._tasks.values()):
            if task and not task.done():
                task.cancel()
        self._tasks.clear()
        self._running.clear()

    # ---------- 主循环 ----------
    async def _loop(self, run_id: str) -> None:
        try:
            while True:
                run, nodes = self._load(run_id)
                if run is None or run["status"] in TERMINAL:
                    return
                if run["status"] == "stopped":
                    return

                if self._open_gate(run_id):
                    self._set_run(run_id, status="waiting_human")
                    return
                if any(n["status"] == "waiting_human" for n in nodes):
                    self._set_run(run_id, status="waiting_human")
                    return

                ready = [
                    n for n in nodes
                    if n["status"] in ("pending", "ready") and self._deps_done(n, nodes)
                ]
                if not ready:
                    if all(n["status"] in ("done", "skipped") for n in nodes):
                        await self._finish(run_id)
                    elif any(n["status"] == "failed" for n in nodes):
                        await self._fail(run_id, "存在失败节点，且没有可重试路径")
                    else:
                        await self._fail(run_id, "调度死锁：存在依赖未满足的节点")
                    return

                self._set_run(run_id, status="running")
                wave = min(n["wave"] if n["wave"] is not None else 0 for n in ready)
                batch = [n for n in ready if (n["wave"] if n["wave"] is not None else 0) == wave]
                await asyncio.gather(*(self._run_node(run_id, n) for n in batch))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 编排器自身异常：标记失败，保留堆栈到事件
            await emit(run_id, "run_error", None,
                       {"error": f"编排器异常：{exc}", "trace": traceback.format_exc()[-4000:]})
            self._set_run(run_id, status="failed", error=f"编排器异常：{exc}")
        finally:
            self._running.discard(run_id)

    # ---------- 单个节点 ----------
    async def _run_node(self, run_id: str, node: dict) -> None:
        if node["type"] == "HUMAN_GATE":
            await self._open_human_gate(run_id, node)
            return

        attempt_no = self._next_attempt_no(node["id"])
        attempt_id = self._create_attempt(node["id"], attempt_no)
        self._update_node(node["id"], status="running")
        await self._enter_phase(run_id, node)
        await emit(run_id, "node_started", node["id"], {
            "node_key": node["node_key"], "type": node["type"],
            "name": node["name"], "attempt": attempt_no,
        })
        ctx = NodeContext(
            run_id=run_id, node_id=node["id"], node_key=node["node_key"],
            type=node["type"], name=node["name"], phase=node["phase"],
            config=dict(node["config"] or {}), assignee=dict(node["assignee"] or {}),
            attempt_id=attempt_id, attempt_no=attempt_no,
            feedback=(node["config"] or {}).get("feedback"),
            idea=self._idea(run_id),
        )
        try:
            result = await execute_node(ctx)
        except Exception as exc:
            message, fatal = classify_failure(exc)
            await self._handle_failure(run_id, node, attempt_id, attempt_no, message, fatal)
            return

        self._finish_attempt(attempt_id)
        self._update_node(node["id"], status="done", clear_feedback=True)
        await emit(run_id, "node_finished", node["id"], {
            "node_key": node["node_key"], "attempt": attempt_no, **result,
        })

    # ---------- 人工门 ----------
    async def _open_human_gate(self, run_id: str, node: dict) -> None:
        from app.engine.gates import request_gate

        self._update_node(node["id"], status="waiting_human")
        gate = request_gate(run_id, node["id"])
        self._set_run(run_id, status="waiting_human")
        await emit(run_id, "gate_requested", node["id"], gate)

    # ---------- 失败回流 ----------
    async def _handle_failure(self, run_id: str, node: dict, attempt_id: int,
                              attempt_no: int, message: str, fatal: bool) -> None:
        self._fail_attempt(attempt_id, message)
        await emit(run_id, "node_failed", node["id"], {
            "node_key": node["node_key"], "attempt": attempt_no, "error": message,
        })
        if not fatal and attempt_no < node["max_attempts"]:
            self._update_node(node["id"], status="pending", feedback=message)
            await emit(run_id, "node_log", node["id"], {
                "level": "warn",
                "message": f"第 {attempt_no}/{node['max_attempts']} 次尝试失败，自动带反馈重试：{message[:300]}",
            })
            return

        from app.engine.gates import request_gate

        self._update_node(node["id"], status="waiting_human", feedback=message,
                          extra={"escalation": True, "escalation_reason": message})
        gate = request_gate(run_id, node["id"], feedback=message)
        self._set_run(run_id, status="waiting_human")
        lesson_for(NodeContext(
            run_id=run_id, node_id=node["id"], node_key=node["node_key"], type=node["type"],
            name=node["name"], phase=node["phase"], config=node["config"] or {},
            assignee=node["assignee"] or {}, attempt_id=attempt_id, attempt_no=attempt_no,
        ), message)
        await emit(run_id, "gate_requested", node["id"], {
            **gate, "reason": "达到最大重试次数，转人工", "escalation": True,
        })

    # ---------- 门决策入口（供 API 调用） ----------
    async def decide_gate(self, *, gate_id: int, action: str, feedback: str | None = None,
                          target_node: str | None = None, decided_by: str = "human",
                          artifact_kind: str | None = None, artifact_body: str | None = None) -> dict:
        result: dict = {}
        edited: dict | None = None
        if artifact_kind and artifact_body:
            from app.artifacts.store import write_artifact

            edited = write_artifact(
                self._run_of_gate(gate_id), artifact_kind, artifact_body,
                created_by=decided_by, status="approved",
            )
        result = _decide_gate(gate_id=gate_id, action=action, feedback=feedback,
                              target_node=target_node, decided_by=decided_by)
        if edited:
            result["edited_artifact"] = edited
        await emit(result["run_id"], "gate_decided", None, result)
        if action == "reject" and result.get("reset_nodes"):
            await emit(result["run_id"], "nodes_reset", None,
                       {"nodes": result["reset_nodes"], "target": target_node})
        self.kick(result["run_id"])
        return result

    # ---------- 恢复 ----------
    async def recover(self) -> None:
        """W1 必做：进程重启后恢复挂起/运行中的 run（不改变待决门）。"""
        with session_scope() as s:
            runs = [
                (r.id, r.status)
                for r in s.query(Run).filter(Run.status.in_(["created", "running", "waiting_human"])).all()
            ]
        for run_id, status in runs:
            reset = 0
            with session_scope() as s:
                for row in (
                    s.query(NodeAttempt)
                    .join(Node, Node.id == NodeAttempt.node_id)
                    .filter(Node.run_id == run_id, NodeAttempt.status == "running")
                    .all()
                ):
                    row.status = "failed"
                    row.error = "进程重启，未回执的 attempt 作废"
                    row.finished_at = _now()
                for node in s.query(Node).filter(Node.run_id == run_id, Node.status == "running").all():
                    node.status = "pending"
                    reset += 1
            if reset:
                await emit(run_id, "run_recovered", None, {"reset_nodes": reset})
            if status == "waiting_human":
                with session_scope() as s:
                    open_gate = (
                        s.query(Gate)
                        .filter(Gate.run_id == run_id, Gate.status == "open")
                        .first()
                    )
                    waiting = (
                        s.query(Node)
                        .filter(Node.run_id == run_id, Node.status == "waiting_human")
                        .order_by(Node.order_index.asc())
                        .first()
                    )
                if open_gate is None and waiting is not None:
                    from app.engine.gates import request_gate

                    request_gate(run_id, waiting.id, feedback=(waiting.config_json or {}).get("feedback"))
                continue
            self.start(run_id)

    # ---------- 读库辅助 ----------
    def _load(self, run_id: str) -> tuple[dict | None, list[dict]]:
        with session_scope() as s:
            run = s.get(Run, run_id)
            if run is None:
                return None, []
            nodes = (
                s.query(Node)
                .filter(Node.run_id == run_id)
                .order_by(Node.order_index.asc(), Node.id.asc())
                .all()
            )
            run_dict = {
                "id": run.id, "status": run.status, "current_phase": run.current_phase,
                "budget_tokens": run.budget_tokens, "error": run.error,
            }
            return run_dict, [
                {
                    "id": n.id, "node_key": n.node_key, "type": n.type, "name": n.name,
                    "phase": n.phase, "status": n.status, "assignee": n.assignee_json or {},
                    "deps": list(n.deps_json or []), "config": n.config_json or {},
                    "max_attempts": n.max_attempts, "wave": n.wave,
                }
                for n in nodes
            ]

    def _definition(self, run_id: str) -> dict:
        with session_scope() as s:
            run = s.get(Run, run_id)
            if run is None:
                return {}
            wf = s.get(Workflow, run.workflow_id)
            return dict(wf.definition_json or {}) if wf else {}

    def _deps_done(self, node: dict, nodes: list[dict]) -> bool:
        status = {n["node_key"]: n["status"] for n in nodes}
        return all(status.get(dep) in ("done", "skipped") for dep in node["deps"])

    def _open_gate(self, run_id: str) -> bool:
        with session_scope() as s:
            return (
                s.query(Gate)
                .filter(Gate.run_id == run_id, Gate.status == "open")
                .first()
                is not None
            )

    def _idea(self, run_id: str) -> str:
        with session_scope() as s:
            run = s.get(Run, run_id)
            return (run.idea_text or "") if run else ""

    def _run_of_gate(self, gate_id: int) -> str:
        with session_scope() as s:
            gate = s.get(Gate, gate_id)
            if gate is None:
                raise GateError("门不存在")
            return gate.run_id

    def _next_attempt_no(self, node_id: int) -> int:
        with session_scope() as s:
            last = (
                s.query(NodeAttempt)
                .filter(NodeAttempt.node_id == node_id)
                .order_by(NodeAttempt.attempt_no.desc())
                .first()
            )
            return (last.attempt_no + 1) if last else 1

    def _create_attempt(self, node_id: int, attempt_no: int) -> int:
        with session_scope() as s:
            row = NodeAttempt(node_id=node_id, attempt_no=attempt_no, status="running")
            s.add(row)
            s.flush()
            return row.id

    def _finish_attempt(self, attempt_id: int) -> None:
        with session_scope() as s:
            row = s.get(NodeAttempt, attempt_id)
            if row:
                row.status = "done"
                row.finished_at = _now()

    def _fail_attempt(self, attempt_id: int, error: str) -> None:
        with session_scope() as s:
            row = s.get(NodeAttempt, attempt_id)
            if row:
                row.status = "failed"
                row.error = error[:2000]
                row.finished_at = _now()

    def _update_node(self, node_id: int, *, status: str, feedback: str | None = None,
                     clear_feedback: bool = False, extra: dict | None = None) -> None:
        with session_scope() as s:
            node = s.get(Node, node_id)
            if node is None:
                return
            node.status = status
            config = dict(node.config_json or {})
            if feedback is not None:
                config["feedback"] = feedback
            if clear_feedback:
                config.pop("feedback", None)
            if extra:
                config.update(extra)
            node.config_json = config

    def _set_run(self, run_id: str, *, status: str, error: str | None = None) -> None:
        with session_scope() as s:
            run = s.get(Run, run_id)
            if run is None or run.status in TERMINAL:
                return
            run.status = status
            if error is not None:
                run.error = error
            if status == "running":
                run.error = None

    async def _enter_phase(self, run_id: str, node: dict) -> None:
        with session_scope() as s:
            run = s.get(Run, run_id)
            if run is None or run.current_phase == node["phase"]:
                return
            run.current_phase = node["phase"]
        definition = self._definition(run_id)
        names = phase_names(definition)
        await emit(run_id, "phase_entered", node["id"], {
            "phase": node["phase"], "name": names.get(node["phase"], node["phase"]),
        })

    async def _finish(self, run_id: str) -> None:
        with session_scope() as s:
            run = s.get(Run, run_id)
            if run is None or run.status in TERMINAL:
                return
            run.status = "finished"
            run.finished_at = _now()
        await emit(run_id, "run_finished", None, {"status": "finished"})

    async def _fail(self, run_id: str, reason: str) -> None:
        with session_scope() as s:
            run = s.get(Run, run_id)
            if run is None or run.status in TERMINAL:
                return
            run.status = "failed"
            run.error = reason
            run.finished_at = _now()
        await emit(run_id, "run_error", None, {"error": reason})


orchestrator = Orchestrator()
