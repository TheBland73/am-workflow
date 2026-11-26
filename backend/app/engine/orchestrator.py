"""编排器：拓扑波次调度、人工门挂起、失败回流、断点恢复（设计规格 §5.2–§5.5）。

编排器是程序，不是 LLM：它只负责推状态、发事件、做判定。
"""
from __future__ import annotations

import asyncio
import random
import traceback
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db.base import session_scope
from app.db.models import Gate, Node, NodeAttempt, Run, Task, Workflow
from app.engine.events import emit
from app.engine.feedback import with_feedback
from app.engine.gates import GateError, decide_gate as _decide_gate, gate_payload
from app.engine.runners import NodeContext, NodeFailure, classify_failure, execute_node, lesson_for
from app.engine.templates import phase_names
from app.engine import judge

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

                now = _now()
                ready = [
                    n for n in nodes
                    if n["status"] in ("pending", "ready")
                    and self._deps_done(n, nodes)
                    and self._retry_ready(n, now)
                ]
                if not ready:
                    # 有节点处于退避等待：睡到最早的重试点，而不是空转或误判死锁。
                    wait = self._next_retry_wait(nodes, now)
                    if wait is not None:
                        await asyncio.sleep(wait)
                        continue
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
                slots = self._slots()
                await asyncio.gather(*(self._run_node(run_id, n, slots) for n in batch))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 编排器自身异常：标记失败，保留堆栈到事件
            await emit(run_id, "run_error", None,
                       {"error": f"编排器异常：{exc}", "trace": traceback.format_exc()[-4000:]})
            self._set_run(run_id, status="failed", error=f"编排器异常：{exc}")
        finally:
            self._running.discard(run_id)

    # ---------- 单个节点 ----------
    async def _run_node(self, run_id: str, node: dict,
                        slots: asyncio.Semaphore | None = None) -> None:
        if node["type"] == "HUMAN_GATE":
            decider = judge.gate_decider(run_id, dict(node.get("config") or {}),
                                         node["node_key"])
            if decider == "auto":
                await self._auto_pass_gate(run_id, node)
            elif decider == "model":
                await self._judge_gate(run_id, node)
            else:
                await self._open_human_gate(run_id, node)
            return
        # 闸门放在建 attempt 之前：排队中的节点不应显示 running，也不应吃掉超时预算。
        async with (slots or self._slots()):
            await self._run_attempt(run_id, node)

    def _slots(self) -> asyncio.Semaphore:
        """事件循环级的全局并发闸门（同一进程内多个 run 共享 AM_NODE_CONCURRENCY）。

        挂在 loop 对象上而不是模块全局：既不跨 loop 复用（测试/多 loop 安全），
        也能在设置变更后按新上限重建。
        """
        loop = asyncio.get_running_loop()
        size = max(1, settings.node_concurrency)
        try:
            sem = getattr(loop, "_am_node_slots", None)
            if sem is None or getattr(loop, "_am_node_slots_size", None) != size:
                sem = asyncio.Semaphore(size)
                setattr(loop, "_am_node_slots", sem)
                setattr(loop, "_am_node_slots_size", size)
            return sem
        except AttributeError:
            # uvloop 等不允许给 loop 挂属性的实现：退化为本次调用一个闸门，仍限制本 run 的并发。
            return asyncio.Semaphore(size)

    async def _run_attempt(self, run_id: str, node: dict) -> None:
        attempt_no = self._next_attempt_no(node["id"])
        attempt_id = self._create_attempt(node["id"], attempt_no)
        self._update_node(node["id"], status="running", drop=["retry_at"])
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
        timeout = self._timeout_for(node)
        heartbeat = asyncio.create_task(self._heartbeat(attempt_id))
        try:
            if timeout > 0:
                result = await asyncio.wait_for(execute_node(ctx), timeout)
            else:
                result = await execute_node(ctx)
        except asyncio.CancelledError:
            # stop() / 进程退出：留下可诊断的 attempt 收据，节点回到可重入的 pending。
            self._fail_attempt(attempt_id, "运行被取消，attempt 未回执")
            self._update_node(node["id"], status="pending")
            raise
        except TimeoutError:
            await self._handle_failure(
                run_id, node, attempt_id, attempt_no,
                f"节点执行超时（{timeout:g}s，可用节点 config.timeout_s 覆盖）", False,
            )
            return
        except Exception as exc:
            message, fatal = classify_failure(exc)
            await self._handle_failure(run_id, node, attempt_id, attempt_no, message, fatal)
            return
        finally:
            heartbeat.cancel()

        self._finish_attempt(attempt_id)
        if result.get("suspended"):
            # 会议在确认点挂起：门、节点与运行的状态已由会议运行器置好，这里只补一条事件
            checkpoint = result.get("checkpoint") or {}
            await emit(run_id, "meeting_suspended", node["id"], {
                "meeting_id": result.get("meeting_id"), "gate_id": checkpoint.get("id"),
                "kind": checkpoint.get("kind")})
            return
        if result.get("reroute"):
            await self._handle_reroute(run_id, node, result["reroute"])
            return
        if node["type"] == "REVIEW" and result.get("verdict") == "block":
            await self._handle_review_block(run_id, node, attempt_id, attempt_no, result)
            return
        self._update_node(node["id"], status="done", clear_feedback=True,
                          drop=["retry_at", "retry_count"])
        await emit(run_id, "node_finished", node["id"], {
            "node_key": node["node_key"], "attempt": attempt_no, **result,
        })

    # ---------- 人工门 ----------
    async def _open_human_gate(self, run_id: str, node: dict) -> None:
        from app.engine.gates import request_gate

        hint = ""
        if dict(node.get("config") or {}).get("check"):
            try:
                hint = judge.hint_for_gate(run_id, node)
            except Exception:
                hint = ""
        self._update_node(node["id"], status="waiting_human",
                          extra={"hint": hint} if hint else None,
                          drop=None if hint else ["hint"])
        gate = request_gate(run_id, node["id"])
        self._set_run(run_id, status="waiting_human")
        await emit(run_id, "gate_requested", node["id"], gate)

    async def _auto_pass_gate(self, run_id: str, node: dict) -> None:
        """判定者是"自动"：不调模型、也不开门，直接完成并批准工件（详设 §5.3）。"""
        config = dict(node.get("config") or {})
        key = judge.checkpoint_key(config, node["node_key"])
        with session_scope() as s:
            gate = Gate(run_id=run_id, node_id=node["id"], status="approved", kind=key,
                        payload_json={"title": config.get("title") or node["name"]},
                        decision_json={"action": "approve", "option": "auto"},
                        decided_by="program", decided_at=_now())
            s.add(gate)
            s.flush()
            gate_id = gate.id
            row = s.get(Node, node["id"])
            if row is not None:
                row.status = "done"
            run = s.get(Run, run_id)
            if run is not None:
                run.status = "running"
                run.error = None
        artifact = config.get("artifact")
        if artifact:
            from app.artifacts.store import set_status

            set_status(run_id, artifact, "approved")
        decision = {"action": "approve", "option": "auto"}
        await emit(run_id, "checkpoint_decided", node["id"], {
            "gate_id": gate_id, "kind": key, "decided_by": "program", "decision": decision})
        await emit(run_id, "gate_decided", node["id"], {
            "gate_id": gate_id, "run_id": run_id, "status": "approved", "action": "approve",
            "feedback": None, "target_node": None, "reset_nodes": [],
            "decided_by": "program"})

    async def _judge_gate(self, run_id: str, node: dict) -> str:
        """判定者是"模型"：为这次判定建一条执行记录，调用裁决模型；判不了就留给人。"""
        attempt_no = self._next_attempt_no(node["id"])
        attempt_id = self._create_attempt(node["id"], attempt_no)
        self._update_node(node["id"], status="running", drop=["retry_at"])
        _, nodes = self._load(run_id)
        deps = [item for item in nodes if item["node_key"] in (node.get("deps") or [])]
        try:
            result = await judge.judge_gate(run_id, node, deps, attempt_id)
        except asyncio.CancelledError:
            self._fail_attempt(attempt_id, "模型判定被取消")
            raise
        except Exception as exc:                      # 判不了就留给人，不拖垮整场运行
            self._fail_attempt(attempt_id, f"模型判定失败：{exc}"[:2000])
            return await self._gate_to_human(run_id, node, f"模型判定失败：{exc}")
        self._finish_attempt(attempt_id)
        await emit(run_id, "node_log", node["id"], {
            "level": "warn" if result["action"] == "open" else "info",
            "message": f"确认点判定：{result['action']}——{result.get('reason') or ''}"[:300]})
        if result["action"] == "open":
            with session_scope() as s:
                gate = (s.query(Gate)
                        .filter(Gate.node_id == node["id"], Gate.status == "open")
                        .order_by(Gate.id.desc()).first())
                body = gate_payload(s, gate) if gate is not None else None
            if body is not None:
                await emit(run_id, "gate_requested", node["id"], {
                    **body, "reason": result.get("reason") or "模型判定无效，转人工"})
            self._set_run(run_id, status="waiting_human")
        return result["action"]

    async def _gate_to_human(self, run_id: str, node: dict, reason: str) -> str:
        """判不了就留给人：已有决定的不动，否则保证有一扇开着的门，原因写进 hint。"""
        with session_scope() as s:
            latest = (s.query(Gate).filter(Gate.node_id == node["id"])
                      .order_by(Gate.id.desc()).first())
            action = str((latest.decision_json or {}).get("action") or "") if latest else ""
            is_open = latest is not None and latest.status == "open"
        if action in ("approve", "reject"):
            return action
        self._update_node(node["id"], status="waiting_human", extra={"hint": reason})
        if not is_open:
            from app.engine.gates import request_gate
            request_gate(run_id, node["id"])
        with session_scope() as s:
            gate = (s.query(Gate)
                    .filter(Gate.node_id == node["id"], Gate.status == "open")
                    .order_by(Gate.id.desc()).first())
            body = gate_payload(s, gate) if gate is not None else None
        if body is not None:
            await emit(run_id, "gate_requested", node["id"], {**body, "reason": reason})
        self._set_run(run_id, status="waiting_human")
        await emit(run_id, "node_log", node["id"], {
            "level": "error", "message": reason[:300]})
        return "open"

    # ---------- 失败回流 ----------
    async def _handle_failure(self, run_id: str, node: dict, attempt_id: int,
                              attempt_no: int, message: str, fatal: bool) -> None:
        self._fail_attempt(attempt_id, message)
        retry = (not fatal) and attempt_no < node["max_attempts"]
        await emit(run_id, "node_failed", node["id"], {
            "node_key": node["node_key"], "attempt": attempt_no, "error": message,
            "will_retry": retry,
        })
        if retry:
            delay = self._retry_delay(attempt_no)
            seq = int((node["config"] or {}).get("feedback_seq") or 0)
            extra: dict = {"retry_count": attempt_no, "feedback_kind": "auto_retry",
                           "feedback_seq": seq + 1}
            if delay > 0:
                extra["retry_at"] = (_now() + timedelta(seconds=delay)).isoformat()
            self._update_node(node["id"], status="pending", feedback=message, extra=extra)
            await emit(run_id, "node_log", node["id"], {
                "level": "warn",
                "message": (
                    f"第 {attempt_no}/{node['max_attempts']} 次尝试失败，"
                    f"{delay:.1f}s 后带反馈重试：{message[:300]}"
                ),
            })
            if delay > 0:
                await emit(run_id, "node_retry_scheduled", node["id"], {
                    "node_key": node["node_key"], "attempt": attempt_no,
                    "delay_s": round(delay, 3), "next_attempt": attempt_no + 1,
                })
            return

        from app.engine.gates import request_gate

        self._update_node(node["id"], status="waiting_human", feedback=message,
                          drop=["retry_at", "feedback_kind"],
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

    # ---------- 定点回流 ----------
    async def _handle_reroute(self, run_id: str, node: dict, instruction: dict) -> None:
        """把指令里的节点定点重开并带反馈（不级联），当前节点轮次加一；轮次用尽转人工。

        评审、确认这类循环靠它承载：评审会议依赖讨论会议，所以讨论会议重跑完成之前，
        评审会议不会被调度。extra_round 为真表示人已同意再评一轮：轮次和上限一起加一。
        """
        config = dict(node["config"] or {})
        try:
            round_no = max(1, int(config.get("round") or 1))
            max_rounds = max(1, int(config.get("max_rounds") or 3))
        except (TypeError, ValueError):
            round_no, max_rounds = 1, 3
        targets = [str(key) for key in (instruction.get("targets") or [])]
        feedback = str(instruction.get("feedback") or "")
        extra_round = bool(instruction.get("extra_round"))

        if round_no >= max_rounds and not extra_round:
            reason = f"回流已达 {max_rounds} 轮：{feedback[:500]}"
            self._update_node(node["id"], status="waiting_human", feedback=reason,
                              drop=["retry_at", "feedback_kind"],
                              extra={"escalation": True, "escalation_reason": reason})
            from app.engine.gates import request_gate

            gate = request_gate(run_id, node["id"], feedback=reason)
            self._set_run(run_id, status="waiting_human")
            await emit(run_id, "node_rerouted", node["id"], {
                "from": node["node_key"], "targets": targets, "round": round_no,
                "max_rounds": max_rounds, "escalated": True})
            await emit(run_id, "gate_requested", node["id"],
                       {**gate, "reason": "回流轮次用尽，转人工", "escalation": True})
            return

        bump = {"round": round_no + 1}
        if extra_round:
            bump["max_rounds"] = max(max_rounds, round_no + 1)
        self_feedback = str(instruction.get("self_feedback") or "").strip()
        if self_feedback:
            updated = with_feedback(config, self_feedback, "reroute_self")
            self._update_node(node["id"], status="pending", drop=["retry_at"], extra={
                **bump, "feedback": updated["feedback"],
                "feedback_kind": updated["feedback_kind"],
                "feedback_seq": updated["feedback_seq"]})
        else:
            self._update_node(node["id"], status="pending", extra=bump,
                              drop=["retry_at", "feedback", "feedback_kind"])
        _, nodes = self._load(run_id)
        reset: list[str] = []
        for other in nodes:
            if other["node_key"] in targets and other["status"] != "running":
                updated = with_feedback(other["config"], feedback, "reroute")
                self._update_node(other["id"], status="pending",
                                  extra={"feedback": updated["feedback"],
                                         "feedback_kind": updated["feedback_kind"],
                                         "feedback_seq": updated["feedback_seq"]},
                                  drop=["retry_at", "retry_count"])
                reset.append(other["node_key"])
        await emit(run_id, "node_rerouted", node["id"], {
            "from": node["node_key"], "targets": reset, "round": round_no + 1,
            "max_rounds": bump.get("max_rounds", max_rounds), "escalated": False})
        skipped = [key for key in targets if key not in reset]
        if skipped:
            await emit(run_id, "node_log", node["id"], {
                "level": "warn",
                "message": f"回流目标未重置（正在运行或不存在）：{'、'.join(skipped)}"})

    # ---------- 评审回流 ----------
    async def _handle_review_block(self, run_id: str, node: dict, attempt_id: int,
                                   attempt_no: int, result: dict) -> None:
        """评审阻塞：回退对应 dev 任务并重跑 verify/review；轮次用尽则转人工门。"""
        config = dict(node.get("config") or {})
        round_no = int(config.get("round") or 1)
        max_rounds = max(1, int(config.get("max_rounds") or 2))
        summary = str(result.get("summary") or "评审未通过")
        _, nodes = self._load(run_id)
        dev_nodes = {
            n["node_key"][4:]: n for n in nodes if n["node_key"].startswith("dev_")
        }
        targets = [str(uid) for uid in (result.get("targets") or [])]
        targets = [uid for uid in targets if uid in dev_nodes]
        if not targets:
            targets = [uid for uid, n in dev_nodes.items() if n["status"] == "done"]
        message = summary
        if result.get("blocking"):
            message = f"{summary}（{result.get('blocking')} 个阻塞项）"

        if round_no >= max_rounds:
            reject = f"评审连续 {round_no} 轮未通过，返工轮次已用尽：{summary}"
            self._update_node(
                node["id"], status="waiting_human", feedback=reject, drop=["retry_at"],
                extra={"escalation": True, "escalation_reason": reject},
            )
            from app.engine.gates import request_gate

            gate = request_gate(run_id, node["id"], feedback=reject)
            self._set_run(run_id, status="waiting_human")
            await emit(run_id, "review_blocked", node["id"], {
                "round": round_no, "max_rounds": max_rounds, "targets": targets,
                "summary": summary[:500], "escalated": True,
            })
            await emit(run_id, "gate_requested", node["id"], {
                **gate, "reason": "评审阻塞且返工轮次用尽", "escalation": True,
            })
            lesson_for(NodeContext(
                run_id=run_id, node_id=node["id"], node_key=node["node_key"],
                type=node["type"], name=node["name"], phase=node["phase"],
                config=config, assignee=node.get("assignee") or {},
                attempt_id=attempt_id, attempt_no=attempt_no,
            ), reject)
            return

        # 回退：dev 目标 + verify + review 自身（review 轮次 +1）。
        self._update_node(node["id"], status="pending", feedback=message,
                          extra={"round": round_no + 1}, drop=["retry_at"])
        for other in nodes:
            if other["node_key"] == "verify" and other["status"] in ("done", "failed", "running"):
                self._update_node(other["id"], status="pending", drop=["retry_at"])
        with session_scope() as s:
            for uid in targets:
                row = (
                    s.query(Task)
                    .filter(Task.run_id == run_id, Task.task_uid == uid)
                    .one_or_none()
                )
                if row is not None:
                    row.status = "pending"
        for uid in targets:
            self._update_node(dev_nodes[uid]["id"], status="pending",
                              feedback=message, drop=["retry_at"])
        await emit(run_id, "review_blocked", node["id"], {
            "round": round_no, "max_rounds": max_rounds, "targets": targets,
            "issues": int(result.get("issues") or 0),
            "blocking": int(result.get("blocking") or 0),
            "summary": summary[:500], "escalated": False,
        })
        await emit(run_id, "node_log", node["id"], {
            "level": "warn",
            "message": (
                f"评审第 {round_no}/{max_rounds} 轮发现阻塞，已回退 "
                f"{', '.join(targets) or '（无目标）'} 并重跑验证"
            ),
        })

    async def rework_review(self, run_id: str, task_uids: list[str],
                            feedback: str | None = None) -> dict:
        """人工从评审报告回退指定任务（评审面板的「回退选中任务」）。"""
        with session_scope() as s:
            run = s.get(Run, run_id)
            if run is None:
                raise GateError("运行不存在")
            if run.status not in ("waiting_human", "failed"):
                raise GateError("只有等待人工处理或失败的运行才能回退任务")
        _, nodes = self._load(run_id)
        dev_nodes = {
            n["node_key"][4:]: n for n in nodes if n["node_key"].startswith("dev_")
        }
        wanted = [str(uid).strip().upper() for uid in task_uids if str(uid).strip().upper() in dev_nodes]
        if not wanted:
            raise GateError("没有可回退的任务")
        review = next((n for n in nodes if n["type"] == "REVIEW"), None)
        verify = next((n for n in nodes if n["node_key"] == "verify"), None)
        message = (feedback or "").strip() or "人工依据评审意见回退，请修复后重新提交验证。"
        for uid in wanted:
            self._update_node(dev_nodes[uid]["id"], status="pending",
                              feedback=message, drop=["retry_at", "retry_count"])
        if verify is not None:
            self._update_node(verify["id"], status="pending", drop=["retry_at", "retry_count"])
        if review is not None:
            config = dict(review.get("config") or {})
            round_no = int(config.get("round") or 1)
            self._update_node(review["id"], status="pending", feedback=message,
                              extra={"round": round_no + 1}, drop=["retry_at"])
        with session_scope() as s:
            for uid in wanted:
                row = (
                    s.query(Task)
                    .filter(Task.run_id == run_id, Task.task_uid == uid)
                    .one_or_none()
                )
                if row is not None:
                    row.status = "pending"
        self._set_run(run_id, status="running")
        await emit(run_id, "review_rework", review["id"] if review else None, {
            "targets": wanted, "feedback": message[:500],
        })
        self.kick(run_id)
        return {"run_id": run_id, "targets": wanted, "feedback": message}

    # ---------- 门决策入口（供 API 调用） ----------
    async def decide_gate(self, *, gate_id: int, action: str, feedback: str | None = None,
                          target_node: str | None = None, decided_by: str = "human",
                          artifact_kind: str | None = None, artifact_body: str | None = None,
                          data: dict | None = None) -> dict:
        route = str((data or {}).get("route") or "").strip()
        route_spec = self._route_spec(gate_id, route) if action == "approve" and route else None
        result: dict = {}
        edited: dict | None = None
        if artifact_kind and artifact_body:
            from app.artifacts.store import write_artifact

            edited = write_artifact(
                self._run_of_gate(gate_id), artifact_kind, artifact_body,
                created_by=decided_by, status="approved",
            )
        result = _decide_gate(gate_id=gate_id, action=action, feedback=feedback,
                              target_node=target_node, decided_by=decided_by, data=data)
        if edited:
            result["edited_artifact"] = edited
        if route_spec is not None:
            result["skipped_nodes"] = await self._skip_route_nodes(
                result["run_id"], route_spec, route)
        await emit(result["run_id"], "gate_decided", None, result)
        if action == "reject" and result.get("reset_nodes"):
            await emit(result["run_id"], "nodes_reset", None,
                       {"nodes": result["reset_nodes"], "target": target_node})
        self.kick(result["run_id"])
        return result

    def _route_spec(self, gate_id: int, route: str) -> dict:
        """通过时选择去向：必须在落决定之前校验，未知的去向让门保持开着。"""
        with session_scope() as s:
            gate = s.get(Gate, gate_id)
            if gate is None:
                raise GateError("门不存在")
            node = s.get(Node, gate.node_id)
            config = dict(node.config_json or {}) if node is not None else {}
        for item in config.get("routes") or []:
            if str(item.get("key") or "") == route:
                return dict(item)
        raise GateError("未知的去向")

    async def _skip_route_nodes(self, run_id: str, spec: dict, route: str) -> list[str]:
        """把去向里的节点逐个置为 skipped，各发一条带去向的 node_finished。"""
        skipped: list[tuple[int, str]] = []
        with session_scope() as s:
            rows = {row.node_key: row
                    for row in s.query(Node).filter(Node.run_id == run_id).all()}
            for key in [str(item) for item in spec.get("nodes") or []]:
                row = rows.get(key)
                if row is None or row.status in ("done", "skipped", "running"):
                    continue
                row.status = "skipped"
                skipped.append((row.id, key))
        for node_id, key in skipped:
            await emit(run_id, "node_finished", node_id, {
                "node_key": key, "skipped": True, "route": route})
        return [key for _, key in skipped]

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
            # 被取消的运行（节点已回到待调度、没有 running 节点）也要留一条恢复事件：
            # 它同样是"进程重启后由恢复流程重新拉起"，下游要靠这个事件确认恢复发生过。
            if reset or status == "running":
                await emit(run_id, "run_recovered", None,
                           {"reset_nodes": reset, "status": status})
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
            row = NodeAttempt(node_id=node_id, attempt_no=attempt_no, status="running",
                              heartbeat_at=_now())
            s.add(row)
            s.flush()
            return row.id

    # ---------- 超时 / 退避 / 心跳 ----------
    def _timeout_for(self, node: dict) -> float:
        raw = (node.get("config") or {}).get("timeout_s")
        try:
            return float(raw) if raw is not None else float(settings.node_timeout_s)
        except (TypeError, ValueError):
            return float(settings.node_timeout_s)

    def _retry_delay(self, attempt_no: int) -> float:
        """指数退避 + equal jitter（Temporal/Airflow 的 retry policy 简化版）。"""
        base = max(0.0, float(settings.retry_base_s))
        if base <= 0:
            return 0.0
        cap = max(base, float(settings.retry_max_s))
        ceiling = min(base * (2 ** max(0, attempt_no - 1)), cap)
        return round(random.uniform(ceiling / 2, ceiling), 3)

    def _retry_ready(self, node: dict, now: datetime) -> bool:
        raw = (node.get("config") or {}).get("retry_at")
        if not raw:
            return True
        due = self._parse_dt(raw)
        return due is None or due <= now

    def _next_retry_wait(self, nodes: list[dict], now: datetime) -> float | None:
        due: list[datetime] = []
        for n in nodes:
            if n["status"] not in ("pending", "ready") or not self._deps_done(n, nodes):
                continue
            raw = (n.get("config") or {}).get("retry_at")
            dt = self._parse_dt(raw)
            if dt is not None and dt > now:
                due.append(dt)
        if not due:
            return None
        delta = (min(due) - now).total_seconds()
        # 上限 5s：兼顾退避精度与停机/门决策的响应速度。
        return max(0.05, min(delta, 5.0))

    @staticmethod
    def _parse_dt(raw) -> datetime | None:
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    async def _heartbeat(self, attempt_id: int) -> None:
        interval = max(1.0, float(settings.heartbeat_s))
        while True:
            await asyncio.sleep(interval)
            self._touch_attempt(attempt_id)

    def _touch_attempt(self, attempt_id: int) -> None:
        with session_scope() as s:
            row = s.get(NodeAttempt, attempt_id)
            if row and row.status == "running":
                row.heartbeat_at = _now()

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
                     clear_feedback: bool = False, extra: dict | None = None,
                     drop: list[str] | None = None) -> None:
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
            for key in drop or ():
                config.pop(key, None)
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
