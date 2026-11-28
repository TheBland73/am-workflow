"""验收脚本 e2e_demo.py：手动模式按确认点类型提问、运行级配置与默认参数（详设 §14.2）。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "e2e_demo.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("e2e_demo_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


e2e_demo = _load_script()


class FakeResponse:
    def __init__(self, payload: dict | None = None):
        self._payload = payload if payload is not None else {}
        self.status_code = 200
        self.text = ""

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        pass


class FakeClient:
    """记录每次请求的路径与 JSON；GET 返回够用的空结构。"""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict | None]] = []
        self.gets: list[tuple[str, dict | None]] = []

    def post(self, path: str, json: dict | None = None, **_) -> FakeResponse:
        self.posts.append((path, json))
        if path == "/runs":
            return FakeResponse({"run": {"id": "run-1"}})
        return FakeResponse({"status": "approved", "reset_nodes": []})

    def get(self, path: str, params: dict | None = None, **_) -> FakeResponse:
        self.gets.append((path, params))
        return FakeResponse({"items": [], "body": "", "artifacts": [], "nodes": [],
                             "tasks": [], "run": {}, "usage": {}})


def _asker(answers: list[str]):
    pending = iter(answers)
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return next(pending)

    return ask, prompts


def _gate(gate_id: int, kind: str, payload: dict | None = None) -> dict:
    return {"id": gate_id, "kind": kind, "node_key": "gate_x",
            "status": "open", "payload": payload or {}}


def test_clarify_asks_each_question_and_posts_answers() -> None:
    client = FakeClient()
    ask, prompts = _asker(["用邮件", ""])
    gate = _gate(7, "clarify", {"questions": ["首版支持哪些平台？", "是否需要登录？"]})

    e2e_demo.manual_decide(client, gate, ask=ask)

    assert len(client.posts) == 1
    path, body = client.posts[0]
    assert path == "/gates/7/approve"
    assert body["data"]["answers"] == ["用邮件", ""]
    assert "target_node" not in body
    assert "首版支持哪些平台？" in prompts[0] and "是否需要登录？" in prompts[1]
    assert all("答（留空=按假设处理）：" in p for p in prompts)


def test_consensus_continue_sends_guidance_as_feedback() -> None:
    client = FakeClient()
    ask, _ = _asker(["c", "先谈权限"])
    gate = _gate(8, "consensus")

    e2e_demo.manual_decide(client, gate, ask=ask)

    path, body = client.posts[0]
    assert path == "/gates/8/approve"
    assert body["data"]["option"] == "continue"
    assert body["feedback"] == "先谈权限"
    assert "target_node" not in body


def test_roster_reject_has_feedback_and_no_target_node() -> None:
    client = FakeClient()
    ask, _ = _asker(["r", "名单里缺安全视角"])
    gate = _gate(9, "roster")

    e2e_demo.manual_decide(client, gate, ask=ask)

    path, body = client.posts[0]
    assert path == "/gates/9/reject"
    assert body["feedback"] == "名单里缺安全视角"
    assert "target_node" not in body


def test_escalation_reject_has_feedback_and_no_target_node() -> None:
    client = FakeClient()
    ask, _ = _asker(["r", "先按现状再议一轮"])
    gate = _gate(10, "escalation")

    e2e_demo.manual_decide(client, gate, ask=ask)

    path, body = client.posts[0]
    assert path == "/gates/10/reject"
    assert body["feedback"] == "先按现状再议一轮"
    assert "target_node" not in body


def test_run_level_gate_reject_still_asks_target_node() -> None:
    client = FakeClient()
    ask, _ = _asker(["r", "补充验收标准", "requirement"])
    gate = _gate(11, "")

    e2e_demo.manual_decide(client, gate, ask=ask)

    path, body = client.posts[0]
    assert path == "/gates/11/reject"
    assert body["target_node"] == "requirement"


def test_create_run_only_sends_config_when_full_history_requested() -> None:
    client = FakeClient()
    e2e_demo.create_run(client, "想法 A", full_history=True)
    path, body = client.posts[0]
    assert path == "/runs"
    assert body["idea"] == "想法 A"
    assert body["config"]["params"]["discussion"]["full_history"] is True

    client = FakeClient()
    e2e_demo.create_run(client, "想法 B", full_history=False)
    _, body = client.posts[0]
    assert "config" not in body


def test_argparse_defaults_for_timeout_and_full_history() -> None:
    args = e2e_demo.build_parser().parse_args([])
    assert args.timeout == 3600
    assert args.full_history is False

    args = e2e_demo.build_parser().parse_args(["--full-history", "--timeout", "60"])
    assert args.full_history is True
    assert args.timeout == 60
