"""端到端演示脚本：配置 Mock 供应商 → 建 run → 自动走完 5 个人工门。

前置：
1. `.\\.venv\\Scripts\\python.exe scripts\\mock_provider.py --port 8100`
2. `.\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --port 8099`

用法（在 backend 目录）：
    .\\.venv\\Scripts\\python.exe scripts\\e2e_demo.py --auto
    .\\.venv\\Scripts\\python.exe scripts\\e2e_demo.py          # 每个门手动确认
"""
from __future__ import annotations

import argparse
import time

import httpx

DEFAULT_IDEA = (
    "做一个面向小团队的「客户反馈收集与归类」内部工具：成员提交反馈，"
    "系统按主题聚类并给出优先级建议，支持导出周报。"
)


def ensure_provider(client: httpx.Client) -> tuple[int, int]:
    providers = client.get("/providers").json()["items"]
    row = next((p for p in providers if p["name"] == "Mock 供应商"), None)
    if row is None:
        resp = client.post("/providers", json={
            "name": "Mock 供应商", "protocol": "openai_chat",
            "base_url": "http://127.0.0.1:8100/v1", "api_key": "sk-mock-key",
            "timeout_s": 60, "max_concurrency": 4, "enabled": True,
        })
        resp.raise_for_status()
        row = resp.json()
        print(f"[setup] 已创建供应商 #{row['id']} {row['name']}")
    models = client.get("/models", params={"provider_id": row["id"]}).json()["items"]
    model = next((m for m in models if m["model_name"] == "mock-fast"), None)
    if model is None:
        resp = client.post("/models", json={
            "provider_id": row["id"], "model_name": "mock-fast",
            "display_name": "Mock Fast", "context_window": 32768,
            "max_output_tokens": 4096, "cap_tools": True, "cap_json": True,
        })
        resp.raise_for_status()
        model = resp.json()
        print(f"[setup] 已创建模型 #{model['id']} {model['model_name']}")
    test = client.post(f"/providers/{row['id']}/test").json()
    print(f"[setup] 测试连接：{test.get('message')}")
    return row["id"], model["id"]


def snapshot(client: httpx.Client, run_id: str) -> dict:
    resp = client.get(f"/runs/{run_id}")
    resp.raise_for_status()
    return resp.json()


def show_gate(snap: dict, gate: dict) -> None:
    node = next((n for n in snap["nodes"] if n["id"] == gate["node_id"]), None)
    print("\n" + "=" * 66)
    print(f"[门] {node['name'] if node else gate['node_key']}  ({gate['node_key']})")
    print(f"     阶段：{node['phase'] if node else '-'}  状态：{gate['status']}")
    for kind in ("proposal", "requirement", "design", "task"):
        art = next((a for a in snap["artifacts"] if a["kind"] == kind and a["status"] == "active"), None)
        if art:
            print(f"     工件：{kind} v{art['version']}  {art['path']}")
    print("=" * 66)


def decide(client: httpx.Client, gate: dict, action: str, **kwargs) -> None:
    resp = client.post(f"/gates/{gate['id']}/{action}", json=kwargs)
    if resp.status_code >= 400:
        raise SystemExit(f"门决策失败：{resp.status_code} {resp.text}")
    data = resp.json()
    print(f"[门] {action} -> {data.get('status')}", end="")
    if data.get("reset_nodes"):
        print(f"；重置 {len(data['reset_nodes'])} 个节点：{data['reset_nodes']}")
    else:
        print()


def approve_gate(client: httpx.Client, gate: dict) -> None:
    """按确认点的类型作答：会议内的确认点要带决定的内容，工件门直接通过。"""
    kind = gate.get("kind") or ""
    if kind == "clarify":
        questions = (gate.get("payload") or {}).get("questions") or []
        decide(client, gate, "approve", decided_by="demo",
               data={"answers": ["以背景材料为准"] * max(1, len(questions))})
    elif kind == "roster":
        decide(client, gate, "approve", decided_by="demo")
    elif kind == "consensus":
        decide(client, gate, "approve", decided_by="demo", data={"option": "integrate"})
    elif kind == "escalation":
        decide(client, gate, "approve", decided_by="demo", data={"option": "accept"})
    else:
        decide(client, gate, "approve", decided_by="demo")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8099/api")
    parser.add_argument("--idea", default=DEFAULT_IDEA)
    parser.add_argument("--auto", action="store_true", help="自动批准所有门")
    parser.add_argument("--demo-reject", action="store_true",
                        help="在需求门演示一次驳回 + 反馈 + 重跑")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    with httpx.Client(base_url=args.base, timeout=60.0) as client:
        ensure_provider(client)
        resp = client.post("/runs", json={"idea": args.idea, "template_key": "sdlc_v1"})
        resp.raise_for_status()
        run_id = resp.json()["run"]["id"]
        print(f"[run] {run_id} 已创建，idea 已落盘")

        rejected = False
        deadline = time.time() + args.timeout
        last_status = None
        while time.time() < deadline:
            snap = snapshot(client, run_id)
            run = snap["run"]
            if run["status"] != last_status:
                print(f"[run] 状态：{run['status']}  阶段：{run.get('current_phase') or '-'}")
                last_status = run["status"]
            if run["status"] in ("finished", "failed", "stopped"):
                break
            if run["status"] == "waiting_human":
                gate = next((g for g in snap["gates"] if g["status"] == "open"), None)
                if gate is None:
                    time.sleep(0.5)
                    continue
                show_gate(snap, gate)
                if args.demo_reject and not rejected and gate["node_key"] == "gate_requirement":
                    rejected = True
                    decide(client, gate, "reject",
                           feedback="需求规格的 R1 缺少验收标准，请补充 Given/When/Then。",
                           target_node="requirement", decided_by="demo")
                    continue
                if args.auto:
                    approve_gate(client, gate)
                else:
                    action = input("通过(a) / 驳回(r) / 编辑后通过(e)? [a] ").strip().lower() or "a"
                    if action == "r":
                        feedback = input("驳回反馈：").strip()
                        target = input("退回节点 key：").strip()
                        decide(client, gate, "reject", feedback=feedback,
                               target_node=target, decided_by="human")
                    elif action == "e":
                        kind = input("工件类型：").strip()
                        art = client.get(f"/runs/{run_id}/artifacts/{kind}").json()
                        body = art["body"] + "\n\n## 人工补充\n\n（此处由审批人补充。）\n"
                        client.patch(f"/runs/{run_id}/artifacts/{kind}",
                                     json={"body": body, "status": "approved"})
                        decide(client, gate, "approve", decided_by="human")
                    else:
                        approve_gate(client, gate)
                continue
            time.sleep(0.6)

        snap = snapshot(client, run_id)
        run = snap["run"]
        print("\n" + "#" * 66)
        print(f"# 结果：{run['status']}  错误：{run.get('error')}")
        usage = snap["usage"]
        print(f"# 用量：in={usage['tokens_in']} out={usage['tokens_out']} "
              f"剩余预算={usage['remaining']}/{usage['budget']}")
        print("# 工件：")
        for art in snap["artifacts"]:
            if art["status"] == "active":
                print(f"#   - {art['kind']:<12} v{art['version']}  {art['path']}")
        print("# 任务：")
        for task in snap["tasks"]:
            print(f"#   - {task['task_uid']:<6} wave {task['wave']}  {task['status']:<8} {task['title']}")
        print("# 节点：")
        for node in snap["nodes"]:
            print(f"#   - {node['node_key']:<14} {node['status']:<14} attempts={node['attempts']}")
        print("#" * 66)
        if run["status"] == "finished":
            changelog = client.get(f"/runs/{run_id}/artifacts/changelog").json()
            print("\n" + changelog["body"][:1200])
        else:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
