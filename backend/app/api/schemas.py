"""请求/响应模型（设计规格 §5.5）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    protocol: str = "openai_chat"
    base_url: str = ""
    api_key: str | None = None
    extra_headers: dict = Field(default_factory=dict)
    timeout_s: int = 60
    max_concurrency: int = 4
    enabled: bool = True
    preset_key: str | None = None


class ModelImportIn(BaseModel):
    models: list[str] = Field(min_length=1, max_length=200)


class ProviderPatch(BaseModel):
    name: str | None = None
    protocol: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    extra_headers: dict | None = None
    timeout_s: int | None = None
    max_concurrency: int | None = None
    enabled: bool | None = None


class ModelIn(BaseModel):
    provider_id: int
    model_name: str = Field(min_length=1, max_length=160)
    display_name: str = ""
    context_window: int = 32768
    max_output_tokens: int = 4096
    cap_tools: bool = True
    cap_json: bool = True
    cap_vision: bool = False
    price_in: float = 0.0
    price_out: float = 0.0
    enabled: bool = True


class ModelPatch(BaseModel):
    model_name: str | None = None
    display_name: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    cap_tools: bool | None = None
    cap_json: bool | None = None
    cap_vision: bool | None = None
    price_in: float | None = None
    price_out: float | None = None
    enabled: bool | None = None


class TemplatePatch(BaseModel):
    system_prompt: str | None = None
    tools_json: list[str] | None = None
    default_model_id: int | None = None
    max_steps: int | None = None
    stop_condition: list[str] | None = None


class RunIn(BaseModel):
    idea: str = Field(min_length=1, max_length=20000)
    template_key: str = "sdlc_v1"
    budget_tokens: int | None = None


class ArtifactPatch(BaseModel):
    body: str = Field(min_length=1)
    status: str = "reviewed"


class GateDecision(BaseModel):
    feedback: str | None = None
    target_node: str | None = None
    decided_by: str = "human"
    artifact_body: str | None = None
    artifact_kind: str | None = None


class TaskIn(BaseModel):
    task_uid: str
    title: str
    deps: list[str] = Field(default_factory=list)
    design_section: str | None = None
    test_cases: list[str] = Field(default_factory=list)
    assignee: str = "dev"
    wave: int | None = None


class PlanCreate(BaseModel):
    product_name: str = Field(min_length=1, max_length=160)
    source_type: str = "text"  # artifact/upload/text
    source_ref: str | None = None
    design_text: str | None = None


class PlanTaskPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    deps: list[str] | None = None
    design_section: str | None = None
    acceptance: list[str] | None = None
    role_key: str | None = None
    effort: str | None = None
    priority: str | None = None


class PlanTaskCreate(BaseModel):
    title: str = ""
    description: str = ""
    deps: list[str] = Field(default_factory=list)
    design_section: str | None = None
    acceptance: list[str] = Field(default_factory=list)
    role_key: str = "dev"
    effort: str = "M"
    priority: str = "P1"


class PlanCommit(BaseModel):
    mode: str = "save"  # save/run
    status: str = "pending"
    # 可选：本次运行的 token 预算；不传则用系统默认。
    budget_tokens: int | None = None
    # run 模式下追加「代码评审节点」与「验收门」；默认开启，可显式关闭。
    review: bool = True
    accept_gate: bool = True


class PlanLayout(BaseModel):
    """画布坐标：{task_uid: {"x": float, "y": float}}。"""

    positions: dict[str, dict[str, float]] = Field(default_factory=dict)


class PlanDep(BaseModel):
    source: str = Field(min_length=1, max_length=16)
    target: str = Field(min_length=1, max_length=16)


class TaskPatch(BaseModel):
    title: str | None = None
    deps: list[str] | None = None
    design_section: str | None = None
    test_cases: list[str] | None = None
    assignee: str | None = None
    wave: int | None = None
    status: str | None = None


class MemoryPatch(BaseModel):
    content: str | None = None
    status: str | None = None
    subject: str | None = None


class RetryIn(BaseModel):
    feedback: str | None = None
    # 增量字段全部带默认值：旧请求体（仅 feedback）保持兼容。
    reset_downstream: bool = False
    expected_status: str | None = None


class NodeResetIn(BaseModel):
    reset_downstream: bool = True
    expected_status: str | None = None


class NodeSkipIn(BaseModel):
    reason: str | None = None
    cascade_downstream: bool = False
    expected_status: str | None = None


class NodeFeedbackIn(BaseModel):
    # 必填语义由端点校验（空/缺失 → 400 feedback_required，而不是框架 422）。
    feedback: str = Field(default="", max_length=4000)
