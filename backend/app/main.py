"""FastAPI 应用入口。

启动顺序：建表 → 载入模板/角色 → 恢复未完成的 run。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import agent_templates, artifacts, gates, memories, model_entries, nodes
from app.api import providers, runs, tasks
from app.config import settings
from app.db.base import init_db
from app.engine.orchestrator import orchestrator
from app.engine.templates import seed_defaults

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("am-workflow")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    init_db()
    seed_defaults()
    await orchestrator.recover()
    log.info("AM-Workflow 已就绪：data=%s runs=%s", settings.data_dir, settings.runs_dir)
    yield
    await orchestrator.shutdown()


app = FastAPI(title="AM-Workflow", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_PREFIX = "/api"
for module in (providers, model_entries, agent_templates, runs, artifacts, gates,
               tasks, nodes, memories):
    app.include_router(module.router, prefix=API_PREFIX)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "app": settings.app_name, "version": app.version}


# 生产模式：若已构建前端，则同源托管（开发模式由 Vite 代理 /api）。
# 前端是单页应用：/runs/:id 这类深链接要回退到 index.html，静态目录托管做不到。
dist = Path(settings.base_dir) / "frontend" / "dist"
if dist.exists():
    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")
    root = dist.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404, "Not Found")
        if full_path:
            candidate = (root / full_path).resolve()
            if root in candidate.parents and candidate.is_file():
                return FileResponse(candidate)
            if "." in full_path.rsplit("/", 1)[-1]:
                raise HTTPException(404, "Not Found")
        return FileResponse(root / "index.html")
