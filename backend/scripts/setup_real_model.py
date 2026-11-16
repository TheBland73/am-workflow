"""把真实模型接入 AM-Workflow：建供应商 → 建模型 → 绑定角色 →（可选）建运行。

密钥只从环境变量读取，不进命令行历史。前置：API 已在 :8099 启动。

用法（在 backend 目录）：
    $env:DEEPSEEK_API_KEY = "sk-..."
    ..\\.venv\\Scripts\\python.exe scripts\\setup_real_model.py --idea ..\\docs\\tasks\\ledger-idea.txt

    # 只配置、不建运行
    ..\\.venv\\Scripts\\python.exe scripts\\setup_real_model.py --no-run

    # 换别家：--base-url 必须带 /v1 或 /api/v3 这类前缀
    ..\\.venv\\Scripts\\python.exe scripts\\setup_real_model.py ^
        --provider-name OpenAI --base-url https://api.openai.com/v1 --model gpt-4o-mini ^
        --api-key-env OPENAI_API_KEY
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

import httpx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="接入真实模型并可选地创建一次运行")
    parser.add_argument("--api", default=os.environ.get("AM_API", "http://127.0.0.1:8099/api"))
    parser.add_argument("--provider-name", default="DeepSeek")
    parser.add_argument("--base-url", default="https://api.deepseek.com/v1")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--context-window", type=int, default=65536)
    parser.add_argument("--max-output", type=int, default=8192)
    parser.add_argument("--price-in", type=float, default=0.14)
    parser.add_argument("--price-out", type=float, default=0.28)
    parser.add_argument("--idea", type=pathlib.Path, help="需求文本文件；省略则用 --idea-text")
    parser.add_argument("--idea-text", default="做一个命令行记账小工具。")
    parser.add_argument("--budget", type=int, default=300_000)
    parser.add_argument("--template-key", default="sdlc_v1")
    parser.add_argument("--no-run", action="store_true", help="只配置供应商/模型/角色，不建运行")
    parser.add_argument("--skip-test", action="store_true", help="跳过连接测试")
    return parser.parse_args()


def ensure_provider(client: httpx.Client, args: argparse.Namespace, api_key: str) -> dict:
    rows = client.get("/providers").json()["items"]
    row = next((p for p in rows if p["name"] == args.provider_name), None)
    if row is None:
        resp = client.post("/providers", json={
            "name": args.provider_name,
            "protocol": "openai_chat",
            "base_url": args.base_url,
            "api_key": api_key,
            "timeout_s": 120,
            "max_concurrency": 2,
            "enabled": True,
        })
        resp.raise_for_status()
        row = resp.json()
        print(f"[setup] 已创建供应商 #{row['id']} {row['name']} → {row['base_url']}")
    else:
        print(f"[setup] 复用供应商 #{row['id']} {row['name']} → {row['base_url']}")
    return row


def ensure_model(client: httpx.Client, provider_id: int, args: argparse.Namespace) -> dict:
    rows = client.get("/models", params={"provider_id": provider_id}).json()["items"]
    row = next((m for m in rows if m["model_name"] == args.model), None)
    if row is not None:
        print(f"[setup] 复用模型 #{row['id']} {row['model_name']}")
        return row
    resp = client.post("/models", json={
        "provider_id": provider_id,
        "model_name": args.model,
        "display_name": args.model,
        "context_window": args.context_window,
        "max_output_tokens": args.max_output,
        "cap_tools": True,
        "cap_json": True,
        "price_in": args.price_in,
        "price_out": args.price_out,
    })
    resp.raise_for_status()
    row = resp.json()
    print(f"[setup] 已创建模型 #{row['id']} {row['model_name']}")
    return row


def bind_templates(client: httpx.Client, model_id: int) -> None:
    items = client.get("/agent-templates").json()["items"]
    for item in items:
        client.patch(f"/agent-templates/{item['id']}", json={"default_model_id": model_id}).raise_for_status()
    print(f"[setup] 已把 {len(items)} 个角色模板绑定到模型 #{model_id}")


def main() -> int:
    args = parse_args()
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        print(f"[error] 环境变量 {args.api_key_env} 为空；先设置 API Key 再运行", file=sys.stderr)
        return 2
    web = args.api[:-3].rstrip("/") if args.api.endswith("/api") else args.api.rstrip("/")

    with httpx.Client(base_url=args.api, timeout=30) as client:
        try:
            provider = ensure_provider(client, args, api_key)
            model = ensure_model(client, provider["id"], args)
            if not args.skip_test:
                result = client.post(f"/providers/{provider['id']}/test",
                                     params={"model_id": model["id"]}).json()
                print(f"[setup] 连接测试：{result.get('message') or result}")
                if not result.get("ok", True):
                    print("[warn] 连接测试未通过；请检查 Base URL 是否带 /v1、模型名与密钥", file=sys.stderr)
            bind_templates(client, model["id"])

            if args.no_run:
                print(f"[done] 配置完成；打开 {web}/runs 新建运行")
                return 0

            idea = args.idea.read_text(encoding="utf-8") if args.idea else args.idea_text
            resp = client.post("/runs", json={
                "idea": idea,
                "template_key": args.template_key,
                "budget_tokens": args.budget,
            })
            resp.raise_for_status()
            snapshot = resp.json()
            run = snapshot.get("run", snapshot)
            print(f"[done] 运行已创建 #{run['id']}，预算 {args.budget} tokens")
            print(f"       打开 {web}/runs/{run['id']}")
            return 0
        except httpx.HTTPStatusError as exc:
            print(f"[error] {exc.request.method} {exc.request.url} → {exc.response.status_code} "
                  f"{exc.response.text[:500]}", file=sys.stderr)
            return 1
        except httpx.ConnectError:
            print(f"[error] 连不上 {args.api}；先启动 API："
                  f"..\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --port 8099", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
