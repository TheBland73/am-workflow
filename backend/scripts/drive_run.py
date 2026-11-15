"""把一个已存在的 run 驱动到结束（演示/联调用）。

用法（在 backend 目录）：
    ..\\.venv\\Scripts\\python.exe scripts\\drive_run.py --latest --auto --demo-reject
    ..\\.venv\\Scripts\\python.exe scripts\\drive_run.py --run <run_id>
"""
from __future__ import annotations

import argparse
import time

import httpx

EDIT_DESIGN = """# 设计规格：AM-Workflow W1

## 1. 模块划分

人工补充：模块边界按 api / engine / models / artifacts / memory 划分，禁止跨层直接访问。

## 2. 数据模型

核心表与字段见 §4.1；事件表 (run_id, seq) 唯一，状态迁移先写事件。

## 3. 调度与并发

拓扑分波，同波并发，汇合屏障；人工门挂起不占协程。

## 4. 接口设计

REST + SSE；断线先取快照再按 seq 补发。

## 5. 上下文与预算

系统 5%、记忆 10%、工件 25%、历史 30%，输出硬预留。

## 6. 错误处理与恢复

适配器重试一次；重启作废未回执 attempt；待决门保持不变。
"""


def snapshot(client: httpx.Client, run_id: str) -> dict:
    resp = client.get(f"/runs/{run_id}")
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8099/api")
    parser.add_argument("--run", default=None)
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--demo-reject", action="store_true")
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()

    with httpx.Client(base_url=args.base, timeout=60.0) as client:
        run_id = args.run
        if not run_id:
            runs = client.get("/runs").json()["items"]
            if args.latest:
                run_id = runs[0]["id"]
            else:
                active = [r for r in runs if r["status"] not in ("finished", "failed", "stopped")]
                if not active:
                    raise SystemExit("没有活跃 run，请先运行 e2e_demo.py")
                run_id = active[0]["id"]
        print(f"[run] {run_id}")
        rejected = False
        deadline = time.time() + args.timeout
        last = None
        while time.time() < deadline:
            snap = snapshot(client, run_id)
            run = snap["run"]
            if run["status"] != last:
                print(f"[run] {run['status']}  phase={run.get('current_phase')}")
                last = run["status"]
            if run["status"] in ("finished", "failed", "stopped"):
                break
            if run["status"] == "waiting_human":
                gate = next((g for g in snap["gates"] if g["status"] == "open"), None)
                if gate is None:
                    time.sleep(0.4)
                    continue
                print(f"[gate] #{gate['id']} {gate['node_key']}")
                if gate["node_key"] == "gate_requirement" and args.demo_reject and not rejected:
                    rejected = True
                    resp = client.post(f"/gates/{gate['id']}/reject", json={
                        "feedback": "需求缺少「导出周报」的验收标准，请补充 R6 并给出 Given/When/Then。",
                        "target_node": "requirement", "decided_by": "demo",
                    })
                elif gate["node_key"] == "gate_design":
                    resp = client.post(f"/gates/{gate['id']}/approve", json={
                        "decided_by": "demo", "artifact_kind": "design",
                        "artifact_body": EDIT_DESIGN,
                    })
                else:
                    resp = client.post(f"/gates/{gate['id']}/approve",
                                       json={"decided_by": "demo"})
                if resp.status_code >= 400:
                    raise SystemExit(f"门决策失败：{resp.status_code} {resp.text}")
                data = resp.json()
                print(f"       -> {data.get('status')} reset={data.get('reset_nodes')}")
                continue
            time.sleep(0.5)

        snap = snapshot(client, run_id)
        run = snap["run"]
        print(f"\nFINAL {run['status']}  error={run.get('error')}")
        print("artifacts:", [(a["kind"], a["version"]) for a in snap["artifacts"]
                             if a["status"] == "active"])
        print("tasks:", [(t["task_uid"], t["status"]) for t in snap["tasks"]])
        print("nodes:", [(n["node_key"], n["status"], n["attempts"]) for n in snap["nodes"]])
        print("usage:", snap["usage"])
        if run["status"] != "finished":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
