"""ORM 模型，对应设计规格 §4.1 核心表（字段为最小集）。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


class ProviderConfig(Base):
    __tablename__ = "provider_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    protocol: Mapped[str] = mapped_column(String(40), default="openai_chat")
    base_url: Mapped[str] = mapped_column(String(500), default="")
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_headers: Mapped[dict] = mapped_column(JSON, default=dict)
    timeout_s: Mapped[int] = mapped_column(Integer, default=60)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=4)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    models: Mapped[list["ModelEntry"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )


class ModelEntry(Base):
    __tablename__ = "model_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("provider_configs.id", ondelete="CASCADE"))
    model_name: Mapped[str] = mapped_column(String(160))
    display_name: Mapped[str] = mapped_column(String(160), default="")
    context_window: Mapped[int] = mapped_column(Integer, default=32768)
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=4096)
    cap_tools: Mapped[bool] = mapped_column(Boolean, default=True)
    cap_vision: Mapped[bool] = mapped_column(Boolean, default=False)
    cap_json: Mapped[bool] = mapped_column(Boolean, default=True)
    price_in: Mapped[float] = mapped_column(Float, default=0.0)
    price_out: Mapped[float] = mapped_column(Float, default=0.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    provider: Mapped[ProviderConfig] = relationship(back_populates="models")


class AgentTemplate(Base):
    __tablename__ = "agent_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role_key: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    system_prompt: Mapped[str] = mapped_column(Text)
    tools_json: Mapped[list] = mapped_column(JSON, default=list)
    memory_scope: Mapped[str] = mapped_column(String(40), default="agent")
    default_model_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_entries.id", ondelete="SET NULL"), nullable=True
    )
    max_steps: Mapped[int] = mapped_column(Integer, default=12)
    stop_condition: Mapped[list] = mapped_column(JSON, default=list)
    # 内置模板指纹（内部字段，不进 API 序列化）：用于区分「内置定义未变的行」
    # 与「用户经 PATCH 改过的行」，NULL 表示旧库遗留行（见 engine/templates.py）。
    builtin_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    template_key: Mapped[str] = mapped_column(String(80), unique=True)
    definition_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(24), primary_key=True, default=new_run_id)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id"))
    status: Mapped[str] = mapped_column(String(30), default="created")
    current_phase: Mapped[str | None] = mapped_column(String(80), nullable=True)
    idea_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    budget_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Node(Base):
    __tablename__ = "nodes"
    __table_args__ = (UniqueConstraint("run_id", "node_key", name="uq_node_run_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    node_key: Mapped[str] = mapped_column(String(80))
    type: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(160))
    phase: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    assignee_json: Mapped[dict] = mapped_column(JSON, default=dict)
    deps_json: Mapped[list] = mapped_column(JSON, default=list)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    wave: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NodeAttempt(Base):
    __tablename__ = "node_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    model_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_entries.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 心跳：执行期由编排器周期刷新，用于崩溃/僵尸 attempt 判定与耗时诊断。
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    # 可空：这一列是后来用 ALTER TABLE 补的，补列之前就存在的行是 NULL，读取处要兜底
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("run_id", "task_uid", name="uq_task_run_uid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    task_uid: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    deps_json: Mapped[list] = mapped_column(JSON, default=list)
    design_section: Mapped[str | None] = mapped_column(String(120), nullable=True)
    test_cases_json: Mapped[list] = mapped_column(JSON, default=list)
    assignee_json: Mapped[dict] = mapped_column(JSON, default=dict)
    wave: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_event_run_seq"),
        Index("ix_event_run_seq", "run_id", "seq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    type: Mapped[str] = mapped_column(String(60))
    node_id: Mapped[int | None] = mapped_column(
        ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True
    )
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    path: Mapped[str] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by_node: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Gate(Base):
    __tablename__ = "gates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(30), default="open")
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    attempt_id: Mapped[int | None] = mapped_column(
        ForeignKey("node_attempts.id", ondelete="SET NULL"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(30))
    content_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(24), primary_key=True, default=new_run_id)
    product_name: Mapped[str] = mapped_column(String(160), default="")
    source_type: Mapped[str] = mapped_column(String(20), default="text")  # artifact/upload/text
    source_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    design_text: Mapped[str] = mapped_column(Text, default="")
    sections_json: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    model_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_entries.id", ondelete="SET NULL"), nullable=True
    )
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # 画布坐标：{task_uid: {"x": float, "y": float}}，由前端拖拽后回写。
    layout_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PlanTask(Base):
    __tablename__ = "plan_tasks"
    __table_args__ = (UniqueConstraint("plan_id", "task_uid", name="uq_plan_task_uid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), index=True
    )
    task_uid: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    deps_json: Mapped[list] = mapped_column(JSON, default=list)
    design_section: Mapped[str | None] = mapped_column(String(200), nullable=True)
    acceptance_json: Mapped[list] = mapped_column(JSON, default=list)
    role_key: Mapped[str] = mapped_column(String(40), default="dev")
    effort: Mapped[str] = mapped_column(String(10), default="M")
    priority: Mapped[str] = mapped_column(String(10), default="P1")
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlanSnapshot(Base):
    __tablename__ = "plan_snapshots"
    __table_args__ = (UniqueConstraint("plan_id", "version", name="uq_plan_snapshot_ver"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    tasks_json: Mapped[list] = mapped_column(JSON, default=list)
    dag_json: Mapped[dict] = mapped_column(JSON, default=dict)
    waves_json: Mapped[dict] = mapped_column(JSON, default=dict)
    critical_path_json: Mapped[list] = mapped_column(JSON, default=list)
    metrics_json: Mapped[dict] = mapped_column(JSON, default=dict)
    coverage_json: Mapped[dict] = mapped_column(JSON, default=dict)
    validation_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (Index("ix_memory_scope", "scope_type", "scope_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(20))  # session/agent/project/global
    scope_id: Mapped[str] = mapped_column(String(120), default="")
    kind: Mapped[str] = mapped_column(String(20), default="episodic")  # semantic/episodic/procedural
    subject: Mapped[str] = mapped_column(String(200), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    source_event: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Conversation(Base):
    """会话：一段有明确归属的消息日志（工具循环、会议里的一次发言等）。"""
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(24), index=True)
    node_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    scope: Mapped[str] = mapped_column(String(40), default="agent_loop")
    owner: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConversationMessage(Base):
    """会话消息：只追加。kind 为 summary 的消息标记一次压缩的边界。"""
    __tablename__ = "conversation_messages"
    __table_args__ = (UniqueConstraint("conversation_id", "seq", name="uq_conversation_msg_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(20), default="normal")
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
