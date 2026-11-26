"""内置角色模板升级：旧库一次性刷新、用户自定义保护、启动幂等。"""
from __future__ import annotations

import copy

import pytest

from app.agents.templates.roles import ROLES
from app.db.base import init_db, session_scope
from app.db.models import AgentTemplate, ModelEntry, ProviderConfig
from app.engine.templates import (
    builtin_fingerprint,
    role_fingerprint,
    seed_defaults,
    template_fields,
)

ROW_FIELDS = (
    "name",
    "system_prompt",
    "tools_json",
    "memory_scope",
    "max_steps",
    "stop_condition",
    "builtin_hash",
)


def _role(role_key: str) -> dict:
    return next(role for role in ROLES if role["role_key"] == role_key)


def _row(role_key: str) -> AgentTemplate:
    with session_scope() as s:
        return s.query(AgentTemplate).filter(AgentTemplate.role_key == role_key).one()


def _snapshot() -> dict[str, dict]:
    with session_scope() as s:
        return {
            row.role_key: {name: copy.deepcopy(getattr(row, name)) for name in ROW_FIELDS}
            for row in s.query(AgentTemplate).all()
        }


def _restore(snapshot: dict[str, dict]) -> None:
    """把角色模板行恢复到用例开始前的状态（共享临时库，行会累积）。"""
    with session_scope() as s:
        rows = {row.role_key: row for row in s.query(AgentTemplate).all()}
        for role_key, state in snapshot.items():
            row = rows.get(role_key)
            if row is None:
                continue
            for name, value in state.items():
                setattr(row, name, copy.deepcopy(value))
        for role_key in set(rows) - set(snapshot):
            s.delete(rows[role_key])


@pytest.fixture(autouse=True)
def _isolated_template_rows():
    init_db()
    seed_defaults()
    snapshot = _snapshot()
    yield
    _restore(snapshot)


def test_legacy_row_without_hash_is_upgraded_in_place() -> None:
    role = _role("facilitator")
    seed_defaults()
    with session_scope() as s:
        row = s.query(AgentTemplate).filter(AgentTemplate.role_key == "facilitator").one()
        row.system_prompt = "旧版讨论者提示词（PR #12 之前的 discuss_pro）"
        row.builtin_hash = None  # 旧库：列由 _ensure_columns 新加，历史行为 NULL
        rows_before = s.query(AgentTemplate).count()

    seed_defaults()

    with session_scope() as s:
        row = s.query(AgentTemplate).filter(AgentTemplate.role_key == "facilitator").one()
        assert row.system_prompt == role["system_prompt"]
        assert row.builtin_hash == role_fingerprint(role)
        assert len(row.builtin_hash) == 64
        assert s.query(AgentTemplate).count() == rows_before


def test_user_edited_row_is_preserved() -> None:
    seed_defaults()
    with session_scope() as s:
        row = s.query(AgentTemplate).filter(AgentTemplate.role_key == "facilitator").one()
        assert row.builtin_hash  # seed 后已有内置指纹
        original_hash = row.builtin_hash
        row.system_prompt = "CUSTOM"
        row.max_steps = 9

    seed_defaults()

    with session_scope() as s:
        row = s.query(AgentTemplate).filter(AgentTemplate.role_key == "facilitator").one()
        assert row.system_prompt == "CUSTOM"
        assert row.max_steps == 9
        assert row.builtin_hash == original_hash


def test_unedited_row_follows_builtin_definition_change() -> None:
    role = _role("review_chair")
    seed_defaults()
    with session_scope() as s:
        row = s.query(AgentTemplate).filter(AgentTemplate.role_key == "review_chair").one()
        row.system_prompt = "上一版内置质疑提示词"
        # 指纹与字段一致 = 用户没改过，只是内置定义变了
        row.builtin_hash = builtin_fingerprint(template_fields(row))

    seed_defaults()

    with session_scope() as s:
        row = s.query(AgentTemplate).filter(AgentTemplate.role_key == "review_chair").one()
        assert row.system_prompt == role["system_prompt"]
        assert row.builtin_hash == role_fingerprint(role)


def test_upgrade_keeps_model_binding_and_created_at() -> None:
    role = _role("review_chair")
    seed_defaults()
    with session_scope() as s:
        stale = (
            s.query(ProviderConfig)
            .filter(ProviderConfig.name == "seed-template-test")
            .one_or_none()
        )
        if stale is not None:
            s.delete(stale)
            s.flush()
        provider = ProviderConfig(
            name="seed-template-test", protocol="openai_chat",
            base_url="http://127.0.0.1:9/v1",
        )
        s.add(provider)
        s.flush()
        model = ModelEntry(provider_id=provider.id, model_name="seed-template-model")
        s.add(model)
        s.flush()
        model_id = model.id
    try:
        with session_scope() as s:
            row = s.query(AgentTemplate).filter(AgentTemplate.role_key == "review_chair").one()
            row.default_model_id = model_id
            row.system_prompt = "旧版内置质疑提示词"
            row.builtin_hash = None
        created_before = _row("review_chair").created_at

        seed_defaults()

        row = _row("review_chair")
        assert row.system_prompt == role["system_prompt"]  # 仍然升级
        assert row.default_model_id == model_id  # 模型绑定原样保留
        assert row.created_at == created_before  # 创建时间原样保留
    finally:
        with session_scope() as s:
            provider = (
                s.query(ProviderConfig)
                .filter(ProviderConfig.name == "seed-template-test")
                .one_or_none()
            )
            if provider is not None:
                s.delete(provider)


def test_seed_is_idempotent() -> None:
    seed_defaults()
    seed_defaults()
    with session_scope() as s:
        rows = s.query(AgentTemplate).all()
        keys = [row.role_key for row in rows]
        assert len(rows) == len(ROLES)
        assert set(keys) == {role["role_key"] for role in ROLES}
        assert keys.count("facilitator") == 1


def test_retired_unmodified_role_is_removed() -> None:
    with session_scope() as s:
        s.add(AgentTemplate(
            role_key="discuss_con", name="讨论者·质疑", system_prompt="旧内置提示词",
            tools_json=[], memory_scope="agent", max_steps=3, stop_condition=[],
            builtin_hash="seed-hash"))
        s.flush()
        row = s.query(AgentTemplate).filter(AgentTemplate.role_key == "discuss_con").one()
        row.builtin_hash = builtin_fingerprint(template_fields(row))   # 字段与指纹一致：没被用户改过

    seed_defaults()

    with session_scope() as s:
        assert s.query(AgentTemplate).filter(AgentTemplate.role_key == "discuss_con").count() == 0


def test_retired_user_edited_role_is_kept_as_custom() -> None:
    with session_scope() as s:
        s.add(AgentTemplate(
            role_key="discuss_pro", name="讨论者·主张", system_prompt="我自己改过的提示词",
            tools_json=[], memory_scope="agent", max_steps=3, stop_condition=[],
            builtin_hash="a-stale-hash-that-does-not-match-the-fields"))

    seed_defaults()

    with session_scope() as s:
        row = s.query(AgentTemplate).filter(AgentTemplate.role_key == "discuss_pro").one()
        assert row.system_prompt == "我自己改过的提示词"
        assert row.builtin_hash is None      # 保留为自定义角色，之后不再被内置同步触碰


def test_retired_legacy_row_without_hash_is_kept() -> None:
    """旧库里的行没有指纹，分不清改没改过：保留，不删。"""
    with session_scope() as s:
        s.add(AgentTemplate(
            role_key="discuss_mod", name="讨论主持人", system_prompt="旧库里的一行",
            tools_json=[], memory_scope="agent", max_steps=3, stop_condition=[],
            builtin_hash=None))
    seed_defaults()
    with session_scope() as s:
        assert s.query(AgentTemplate).filter(AgentTemplate.role_key == "discuss_mod").count() == 1


def test_new_roles_carry_meeting_metadata() -> None:
    seed_defaults()
    assert _row("review_chair").meta_json["meeting_roles"] == ["chair"]
    assert _row("requester_proxy").meta_json["meeting_roles"] == ["proxy"]
    architect = _row("architect")
    assert architect.meta_json["meeting_roles"] == ["discussion", "review"]
    assert architect.meta_json["description"]
    assert _row("judge").meta_json["tags"] == ["裁决"]
