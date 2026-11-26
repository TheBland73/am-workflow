"""背景材料：一组文本，放在运行目录下的 materials 子目录。

背景材料不是工件，不进版本链：想法是用户的一句话，材料是用户补充的上下文
（需求文档片段、会议记录、约束清单等）。总量有硬上限，超出即拒绝提交；
拼进会议章程时受预算约束，放不下的部分截断，并写明只提供了部分材料。
文件名形如 001-需求片段：前缀是提交的顺序，读取时按它排。
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from app.config import settings
from app.models.budget import estimate_tokens, truncate_to_tokens

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n]')
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TRUNCATED_NOTE = "\n\n（仅提供了部分材料：背景材料超出预算，以上只是前面的一部分）"
_NAME_MAX_BYTES = 200         # 文件名上限 255 字节，留出序号前缀与扩展名

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def _lock(run_id: str) -> threading.Lock:
    """同一运行的材料读写串行化：总量检查与序号分配要在同一个临界区里完成。"""
    with _locks_guard:
        return _locks.setdefault(run_id, threading.Lock())


def _dir(run_id: str) -> Path:
    return settings.runs_dir / run_id / "materials"


def _entries(run_id: str) -> list[tuple[int, str, Path]]:
    """已有的材料：(序号, 名称, 路径)，按提交顺序。"""
    directory = _dir(run_id)
    entries: list[tuple[int, str, Path]] = []
    for path in directory.glob("*.md") if directory.exists() else []:
        seq, _, name = path.stem.partition("-")
        if seq.isdigit():
            entries.append((int(seq), name, path))
    return sorted(entries)


def _name_error(name: str) -> str | None:
    if not 1 <= len(name) <= 80:
        return "材料名称长度须是 1 到 80 个字符"
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError:
        return "材料名称含有无法保存的字符"
    if len(encoded) > _NAME_MAX_BYTES:
        return "材料名称过长"
    if _CONTROL.search(name):
        return "材料名称不能包含控制字符"
    if _BAD_CHARS.search(name):
        return f'材料名称不能包含 \\ / : * ? " < > | 或换行：{name}'
    return None


def free_material_name(used: set[str], start: int = 1) -> str:
    """第一个未被占用的自动名称。"""
    index = max(1, start)
    while f"材料{index}" in used:
        index += 1
    return f"材料{index}"


def check_materials(items: list[tuple[str, str]]) -> list[str]:
    """新建运行时的一次性检查：名称合法、不重名、总量不超限。返回错误列表。"""
    errors = [error for error in (_name_error(name) for name, _ in items) if error]
    names = [name for name, _ in items]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        errors.append("材料名称重复：" + "、".join(duplicates))
    total = sum(len(content) for _, content in items)
    if total > settings.materials_max_chars:
        errors.append(f"背景材料总量不能超过 {settings.materials_max_chars} 字，当前 {total} 字")
    return errors


def list_materials(run_id: str) -> list[dict]:
    return [{"name": name, "chars": len(path.read_text(encoding="utf-8"))}
            for _, name, path in _entries(run_id)]


def total_chars(run_id: str) -> int:
    return sum(item["chars"] for item in list_materials(run_id))


def save_material(run_id: str, name: str, content: str) -> dict:
    """写入一份材料；名称为空时自动编号，重名则覆盖。名称不合法或总量超限时抛 ValueError。"""
    name = (name or "").strip()
    with _lock(run_id):
        entries = _entries(run_id)
        if not name:
            name = free_material_name({item_name for _, item_name, _ in entries},
                                      len(entries) + 1)
        error = _name_error(name)
        if error:
            raise ValueError(error)
        try:
            content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("材料内容含有无法保存的字符") from exc
        target = next((path for _, item_name, path in entries if item_name == name), None)
        replaced = len(target.read_text(encoding="utf-8")) if target else 0
        if total_chars(run_id) - replaced + len(content) > settings.materials_max_chars:
            raise ValueError(f"背景材料总量不能超过 {settings.materials_max_chars} 字")
        _dir(run_id).mkdir(parents=True, exist_ok=True)
        if target is None:
            seq = max((seq for seq, _, _ in entries), default=0) + 1
            target = _dir(run_id) / f"{seq:03d}-{name}.md"
        # 先写临时文件再原子替换：写到一半失败不会毁掉同名旧材料
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, target)
        return {"name": name, "chars": len(content)}


def read_material(run_id: str, name: str) -> str:
    if _name_error(name):
        raise FileNotFoundError(name)
    entry = next((path for _, item_name, path in _entries(run_id) if item_name == name), None)
    if entry is None:
        raise FileNotFoundError(name)
    return entry.read_text(encoding="utf-8")


def read_materials(run_id: str, budget: int) -> tuple[str, bool]:
    """把全部材料拼成一段文本，不超过 budget 个 token（估算）。返回 (文本, 是否截断)。

    截断只取决于材料和预算：同一场会议里每次调用得到的文本相同，不破坏前缀缓存。
    """
    entries = _entries(run_id)
    if not entries:
        return "", False
    joined = "\n\n".join(f"### 材料：{name}\n{path.read_text(encoding='utf-8').strip()}"
                         for _, name, path in entries)
    if estimate_tokens(joined) <= budget:
        return joined, False
    return truncate_to_tokens(joined, budget) + _TRUNCATED_NOTE, True
