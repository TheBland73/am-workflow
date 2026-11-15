"""Agent 工具：读写文件 / 列目录 / 执行命令 / 声明完成（设计规格 §5.7）。"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT = 8000
MAX_READ = 20000
IGNORED_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}


class ToolError(RuntimeError):
    pass


@dataclass
class ToolOutcome:
    output: str
    is_error: bool = False
    finished: bool = False


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.25):]
    return f"{head}\n…（输出过长已截断 {len(text) - limit} 字符）…\n{tail}"


def _safe_path(workspace: Path, rel: str) -> Path:
    rel = (rel or ".").strip().replace("\\", "/")
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        raise ToolError("只允许工作区内的相对路径")
    target = (workspace / rel).resolve()
    root = workspace.resolve()
    if target != root and root not in target.parents:
        raise ToolError("路径越界：只能访问工作区内的文件")
    return target


TOOL_SCHEMAS: list[dict] = [
    {
        "name": "write_file",
        "description": "在工作区内写入（或覆盖）一个文本文件，自动创建父目录。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区的路径，如 app/calc.py"},
                "content": {"type": "string", "description": "完整文件内容"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": "读取工作区内一个文本文件的内容。",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对路径"}},
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": "列出工作区内的文件（递归，最多 200 条）。",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对路径，默认 ."}},
        },
    },
    {
        "name": "run_command",
        "description": "在工作区执行 shell 命令并返回输出与退出码，用于跑测试、编译等。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
                "timeout_s": {"type": "integer", "description": "超时秒数，默认 120，最大 300"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "finish",
        "description": "声明任务完成并给出简短总结。调用后本轮结束。",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "完成情况、验证结果、遗留问题"},
            },
            "required": ["summary"],
        },
    },
]


class Toolbox:
    def __init__(self, workspace: Path, *, allow_shell: bool = True) -> None:
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.allow_shell = allow_shell

    def schemas(self) -> list[dict]:
        return [s for s in TOOL_SCHEMAS if self.allow_shell or s["name"] != "run_command"]

    async def execute(self, name: str, args: dict) -> ToolOutcome:
        try:
            if name == "write_file":
                return self._write(args)
            if name == "read_file":
                return self._read(args)
            if name == "list_files":
                return self._list(args)
            if name == "run_command":
                if not self.allow_shell:
                    return ToolOutcome("该节点不允许执行命令", True)
                return await self._run(args)
            if name == "finish":
                return ToolOutcome(str(args.get("summary") or "完成"), False, True)
            return ToolOutcome(f"未知工具：{name}", True)
        except ToolError as exc:
            return ToolOutcome(str(exc), True)
        except Exception as exc:  # 工具异常回传给模型，而不是炸掉循环
            return ToolOutcome(f"工具执行异常：{exc}", True)

    # ---------- 实现 ----------
    def _write(self, args: dict) -> ToolOutcome:
        path = _safe_path(self.workspace, str(args.get("path") or ""))
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("content 必须是字符串")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        rel = path.relative_to(self.workspace.resolve()).as_posix()
        return ToolOutcome(f"已写入 {rel}（{len(content)} 字符）")

    def _read(self, args: dict) -> ToolOutcome:
        path = _safe_path(self.workspace, str(args.get("path") or ""))
        if not path.exists():
            raise ToolError(f"文件不存在：{args.get('path')}")
        text = path.read_text(encoding="utf-8", errors="replace")
        return ToolOutcome(_truncate(text, MAX_READ))

    def _list(self, args: dict) -> ToolOutcome:
        root = _safe_path(self.workspace, str(args.get("path") or "."))
        if not root.exists():
            raise ToolError(f"目录不存在：{args.get('path')}")
        lines: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
            for filename in filenames:
                full = Path(dirpath) / filename
                try:
                    rel = full.relative_to(self.workspace.resolve()).as_posix()
                except ValueError:
                    continue
                lines.append(f"{rel}  ({full.stat().st_size} B)")
                if len(lines) >= 200:
                    lines.append("…（文件过多，已截断）")
                    return ToolOutcome("\n".join(lines))
        return ToolOutcome("\n".join(lines) or "（空目录）")

    async def _run(self, args: dict) -> ToolOutcome:
        command = str(args.get("command") or "").strip()
        if not command:
            raise ToolError("command 不能为空")
        try:
            timeout = min(300, max(5, int(args.get("timeout_s") or 120)))
        except (TypeError, ValueError):
            timeout = 120
        if command.startswith("python "):
            command = f'"{sys.executable}" ' + command[len("python "):]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        proc = await asyncio.create_subprocess_shell(
            command, cwd=str(self.workspace), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=env,
        )
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolOutcome(f"命令超时（>{timeout}s）：{command}", True)
        output = raw.decode("utf-8", errors="replace")
        code = proc.returncode or 0
        body = _truncate(output) or "（无输出）"
        return ToolOutcome(f"[exit code: {code}]\n{body}", code != 0)
