"""数据库连接与会话。

设计规格 §5.1「DB 并发（M0 开发期配置）」红线：
1. SQLAlchemy 连接池 size=1 —— 单连接天然串行写；
2. connect_args={"check_same_thread": False} —— FastAPI 线程池跨线程借还；
3. busy_timeout=5000 + WAL + synchronous=NORMAL；
4. pool_timeout 调大，避免多线程等单连接偶发超时。
**SSE 生成器绝不持有 session，node_token 不落库。**
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import QueuePool

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(
    settings.db_url,
    poolclass=QueuePool,
    pool_size=1,
    max_overflow=0,
    pool_timeout=60,
    connect_args={"check_same_thread": False, "timeout": 5},
    future=True,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # pragma: no cover
    if not settings.db_url.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(
    bind=engine, class_=Session, autoflush=False, expire_on_commit=False, future=True
)


@contextmanager
def session_scope() -> Iterator[Session]:
    """短借短还。禁止在 with 块内 await。"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    from app.db import models  # noqa: F401  确保 ORM 已注册

    settings.ensure_dirs()
    Base.metadata.create_all(bind=engine)
    _ensure_columns()


def _ensure_columns() -> None:
    """轻量 schema 演进（SQLite 无 Alembic）：启动时幂等补齐增量列。

    只处理「加列」这一类向后兼容变更；已有数据行新列为 NULL，由业务代码兜底。
    """
    if not settings.db_url.startswith("sqlite"):
        return
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    additions: dict[str, dict[str, str]] = {
        "node_attempts": {"heartbeat_at": "DATETIME"},
        "plans": {"layout_json": "JSON"},
        "plan_snapshots": {"coverage_json": "JSON"},
    }
    # 先在事务之外把已有列读出来：SQLite 连接池 size=1，事务内再 inspect 会自锁。
    existing_by_table = {
        table: {col["name"] for col in inspector.get_columns(table)}
        for table in additions
        if table in tables
    }
    with engine.begin() as conn:
        for table, columns in additions.items():
            existing = existing_by_table.get(table)
            if existing is None:
                continue
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
