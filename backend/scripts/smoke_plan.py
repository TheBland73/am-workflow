"""ORCH-01 冒烟：创建计划 → 生成 → 校验 → 提交 save。

用法：backend 下 `..\\.venv\\Scripts\\python.exe scripts\\smoke_plan.py`
"""
from __future__ import annotations

import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8375/api"

DESIGN = """# 客户反馈收集与归类 · 设计规格

## 1 目标与范围
为内容团队提供一个轻量反馈收集页，支持匿名提交与按主题归类。

## 2 数据模型
反馈表包含 id、内容、来源、主题标签、创建时间与处理状态。

## 3 接口设计
提供提交、列表与归类三个接口，错误统一返回 code/message。

## 4 前端交互
表单需要实时字数提示、提交后反馈与失败重试。

## 5 权限与安全
仅管理员可查看全部反馈，匿名提交需要频率限制。

## 6 测试与回归
覆盖提交、归类、权限与限流四类路径，验收命令退出码为 0。

## 7 部署与监控
单进程部署，记录请求量、错误率与慢请求。

## 8 验收与文档
提供演示脚本与 README，覆盖主流程与错误路径。
"""


def main() -> int:
    with httpx.Client(timeout=60) as client:
        created = client.post(
            f"{BASE}/plans",
            json={
                "product_name": "客户反馈收集与归类",
                "source_type": "text",
                "design_text": DESIGN,
            },
        )
        print("create", created.status_code)
        print(created.text[:1200])
        if created.status_code >= 400:
            return 1
        plan_id = created.json()["plan"]["id"]
        started = client.post(f"{BASE}/plans/{plan_id}/generate")
        print("generate", started.status_code, started.text[:300])
        for _ in range(40):
            time.sleep(0.5)
            detail = client.get(f"{BASE}/plans/{plan_id}")
            if detail.status_code != 200:
                print("detail", detail.status_code, detail.text[:500])
                return 1
            data = detail.json()
            status = data["plan"]["status"]
            print("status", status, "tasks", len(data.get("tasks") or []))
            if status in {"valid", "invalid", "failed"}:
                print(json.dumps(data["plan"], ensure_ascii=False, indent=2)[:800])
                if data.get("snapshot"):
                    print("metrics", json.dumps(data["snapshot"].get("metrics"), ensure_ascii=False))
                if status == "valid":
                    committed = client.post(f"{BASE}/plans/{plan_id}/commit", json={"mode": "save"})
                    print("commit", committed.status_code, committed.text[:300])
                return 0 if status == "valid" else 1
    print("timeout")
    return 1


if __name__ == "__main__":
    sys.exit(main())
