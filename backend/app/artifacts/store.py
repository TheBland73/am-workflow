"""工件链落盘：YAML front-matter + 版本（设计规格 §5.6）。"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func

from app.config import settings
from app.db.base import session_scope
from app.db.models import Artifact

ARTIFACT_KINDS = [
    "idea",
    "discussion",
    "proposal",
    "review",
    "requirement",
    "design",
    "task",
    "test",
    "changelog",
]

_META_KEYS = ("id", "kind", "status", "version", "inputs", "created")


def run_dir(run_id: str) -> Path:
    return settings.runs_dir / run_id


def artifacts_dir(run_id: str) -> Path:
    path = run_dir(run_id) / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def workspace_dir(run_id: str) -> Path:
    path = run_dir(run_id) / "workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def render_front_matter(meta: dict, body: str) -> str:
    lines = ["---"]
    for key in _META_KEYS:
        value = meta.get(key)
        if key == "inputs":
            items = value or []
            lines.append(f"inputs: [{', '.join(str(i) for i in items)}]")
        else:
            lines.append(f"{key}: {value}")
    # 工件自己的附加字段（如评审报告的 review_type）跟在固定键之后
    for key, value in meta.items():
        if key not in _META_KEYS and value is not None:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.strip() + "\n"


def parse_front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict = {}
    for line in parts[1].strip().splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if key.strip() == "inputs":
            value = [v.strip() for v in value.strip("[]").split(",") if v.strip()]
        elif key.strip() == "version":
            try:
                value = int(value)
            except ValueError:
                pass
        elif key.strip() == "status":
            value = value or "draft"
        meta[key.strip()] = value
    return meta, parts[2].lstrip("\n")


def latest_artifact(run_id: str, kind: str) -> dict | None:
    with session_scope() as s:
        row = (
            s.query(Artifact)
            .filter(Artifact.run_id == run_id, Artifact.kind == kind)
            .order_by(Artifact.version.desc())
            .first()
        )
        if not row:
            return None
        return {
            "kind": row.kind,
            "path": row.path,
            "version": row.version,
            "status": row.status,
            "created_by_node": row.created_by_node,
        }


def read_artifact(run_id: str, kind: str) -> str | None:
    record = latest_artifact(run_id, kind)
    if not record:
        return None
    path = Path(record["path"])
    if not path.is_absolute():
        path = settings.base_dir / path
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def write_artifact(run_id: str, kind: str, body: str, *, created_by: str | None = None,
                   inputs: list[str] | None = None, status: str = "draft",
                   extra_meta: dict | None = None) -> dict:
    """写一个新版本。extra_meta 是工件自己的附加字段，写进 front-matter，不能覆盖固定的六个键。"""
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"未知工件类型：{kind}")
    with session_scope() as s:
        current = (
            s.query(func.max(Artifact.version))
            .filter(Artifact.run_id == run_id, Artifact.kind == kind)
            .scalar()
            or 0
        )
        version = current + 1
        rows = (
            s.query(Artifact)
            .filter(Artifact.run_id == run_id, Artifact.kind == kind,
                    Artifact.status == "active")
            .all()
        )
        for row in rows:
            row.status = "superseded"
        meta = {
            "id": f"{run_id}-{kind}",
            "kind": kind,
            "status": status,
            "version": version,
            "inputs": inputs or [],
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        meta.update({key: value for key, value in (extra_meta or {}).items() if key not in meta})
        text = render_front_matter(meta, body)
        directory = artifacts_dir(run_id)
        versioned = directory / f"{kind}.v{version}.md"
        versioned.write_text(text, encoding="utf-8")
        (directory / f"{kind}.md").write_text(text, encoding="utf-8")
        rel_path = str(versioned.relative_to(settings.base_dir))
        row = Artifact(run_id=run_id, kind=kind, path=rel_path, version=version,
                       created_by_node=created_by, status="active")
        s.add(row)
        s.flush()
        return {"kind": kind, "version": version, "path": rel_path,
                "created_by_node": created_by, "status": status}


def set_status(run_id: str, kind: str, status: str) -> dict | None:
    """只改当前 active 版本 front-matter 的状态（审批通过用），不产生新版本。"""
    record = latest_artifact(run_id, kind)
    if not record:
        return None
    path = Path(record["path"])
    if not path.is_absolute():
        path = settings.base_dir / path
    if not path.exists():
        return None
    meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
    meta["status"] = status
    text = render_front_matter(meta, body)
    path.write_text(text, encoding="utf-8")
    canonical = artifacts_dir(run_id) / f"{kind}.md"
    if canonical.exists() and canonical != path:
        canonical.write_text(text, encoding="utf-8")
    record["status"] = status
    return record


def list_artifacts(run_id: str) -> list[dict]:
    with session_scope() as s:
        rows = (
            s.query(Artifact)
            .filter(Artifact.run_id == run_id)
            .order_by(Artifact.kind.asc(), Artifact.version.desc())
            .all()
        )
        return [
            {
                "kind": r.kind,
                "path": r.path,
                "version": r.version,
                "status": r.status,
                "created_by_node": r.created_by_node,
                "created_at": r.created_at.isoformat() if r.created_at else "",
            }
            for r in rows
        ]


_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n(.*?)\n```\s*$", re.S)


def strip_outer_fence(text: str) -> str:
    """去掉模型常见的整篇 ```markdown 包裹。"""
    text = text.strip()
    match = _FENCE.match(text)
    return match.group(1).strip() if match else text
