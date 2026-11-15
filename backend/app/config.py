"""AM-Workflow 全局配置。

数据落盘约定（设计规格 §3.1 / §3.3）：
- DB:        <repo>/data/am.db
- 运行数据区: <repo>/runs/<run_id>/{artifacts,workspace,logs}
- 密钥:      <repo>/data/secret.key（或 AM_SECRET_KEY 环境变量注入）
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]  # am-workflow/
DATA_DIR = Path(os.environ.get("AM_DATA_DIR", BASE_DIR / "data"))
RUNS_DIR = Path(os.environ.get("AM_RUNS_DIR", BASE_DIR / "runs"))
TEMPLATES_DIR = BASE_DIR / "templates"
AGENT_TEMPLATES_DIR = Path(__file__).resolve().parent / "agents" / "templates"


class Settings:
    def __init__(self) -> None:
        self.app_name = "AM-Workflow"
        self.base_dir = BASE_DIR
        self.data_dir = DATA_DIR
        self.runs_dir = RUNS_DIR
        self.templates_dir = TEMPLATES_DIR
        self.db_url = os.environ.get(
            "AM_DATABASE_URL", f"sqlite:///{(DATA_DIR / 'am.db').as_posix()}"
        )
        # 开发期默认放行 Vite dev server；生产由 FastAPI 托管构建产物，同源。
        self.cors_origins = os.environ.get(
            "AM_CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173"
        ).split(",")
        self.node_concurrency = int(os.environ.get("AM_NODE_CONCURRENCY", "4"))
        self.default_budget_tokens = int(os.environ.get("AM_RUN_BUDGET_TOKENS", "1200000"))
        self.stream_tokens = os.environ.get("AM_STREAM_TOKENS", "1") != "0"
        self.memory_extract = os.environ.get("AM_MEMORY_EXTRACT", "1") != "0"
        self.max_rework_rounds = 3
        self.secret_key_file = self.data_dir / "secret.key"
        self.secret_key_env = os.environ.get("AM_SECRET_KEY")
        # 离线演示用本地 OpenAI 兼容 mock（不是产品内置供应商）
        self.demo_base_url = os.environ.get("AM_DEMO_BASE_URL", "http://127.0.0.1:8100/v1")

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
