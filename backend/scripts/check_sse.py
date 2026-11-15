"""检查 SSE 事件流：读取若干帧并打印事件类型（联调用）。

用法（在 backend 目录）：
    ..\\.venv\\Scripts\\python.exe scripts\\check_sse.py <run_id>
"""
from __future__ import annotations

import sys

import httpx

BASE = "http://127.0.0.1:8099/api"


def main() -> None:
    run_id = sys.argv[1] if len(sys.argv) > 1 else None
    since = sys.argv[2] if len(sys.argv) > 2 else None
    headers = {"Last-Event-ID": since} if since else {}
    with httpx.Client(base_url=BASE, timeout=30.0) as client:
        if not run_id:
            run_id = client.get("/runs").json()["items"][0]["id"]
        print(f"[sse] run={run_id} since={since or '-'}")
        event_type = None
        seen = 0
        with client.stream("GET", f"/runs/{run_id}/events", headers=headers) as resp:
            print(f"[sse] status={resp.status_code} type={resp.headers.get('content-type')}")
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    event_type = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    seen += 1
                    print(f"[sse] {seen:02d} {event_type} {line[5:][:160]}")
                    if seen >= 12:
                        break
        print(f"[sse] 收到 {seen} 帧")


if __name__ == "__main__":
    main()
