"""开发《前半段重构开发文档》时的辅助工具。开发完成、文档删除时一并删除（见文档的任务 H4）。

文档里每个"整文件"的代码块都紧跟在一条"`路径`："标签行后面。手抄几百行代码容易出错，
这里按标签把围栏代码块原样抽出来写到目标文件；另外提供勾选步骤、定位任务行号和变异检查。

用法（在仓库任意位置执行，解释器用仓库根目录的 .venv）：
    python backend/scripts/front_half_plan_tool.py where <任务>
        打印任务的行号范围（1 起，含首尾）
    python backend/scripts/front_half_plan_tool.py extract <任务> <标签> <目标文件> [第几处]
        把"`标签`："后面紧跟的围栏代码块写到目标文件（同一标签出现多次时用"第几处"选，默认第 1 处）
    python backend/scripts/front_half_plan_tool.py tick <任务> [步骤号 ...]
        勾选该任务的步骤；不给步骤号就全部勾选
    python backend/scripts/front_half_plan_tool.py mutate <源文件> <旧文本> <新文本> <pytest 参数...>
        变异检查：把源文件里的一处代码临时替换掉（旧文本必须恰好出现一次），跑指定的测试，
        然后无论结果如何都原样恢复。源文件相对 backend/ 目录
"""
from __future__ import annotations

import io
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
PLAN = ROOT / "docs" / "tasks" / "前半段重构开发文档.md"


# ---------------- 读文档 ----------------
def _read() -> list[str]:
    return io.open(PLAN, encoding="utf-8", newline="").read().split("\n")


def _task_range(lines: list[str], task: str) -> tuple[int, int]:
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"### Task {task}:"))
    end = next(i for i in range(start + 1, len(lines))
               if lines[i].startswith("### Task ") or lines[i].startswith("## "))
    return start, end


def cmd_where(task: str) -> None:
    start, end = _task_range(_read(), task)
    print(start + 1, end)


def cmd_extract(task: str, label: str, dest: str, occurrence: str | None = None) -> None:
    lines = _read()
    start, end = _task_range(lines, task)
    key = f"`{label}`"
    hits = [i for i in range(start, end) if lines[i].lstrip("\ufeff").strip().startswith(key)]
    wanted = int(occurrence or 1)
    if len(hits) < wanted:
        sys.exit(f"找不到标签行：{key}（要第 {wanted} 处，实际 {len(hits)} 处）")
    opening = hits[wanted - 1] + 1
    while not lines[opening].strip():
        opening += 1
    if not lines[opening].startswith("```"):
        sys.exit(f"标签行 {hits[wanted - 1] + 1} 之后不是围栏代码块")
    closing = next(i for i in range(opening + 1, end) if lines[i].startswith("```"))
    body = [ln.rstrip("\r") for ln in lines[opening + 1:closing]]
    out = (ROOT / dest) if not Path(dest).is_absolute() else Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    io.open(out, "w", encoding="utf-8", newline="\n").write("\n".join(body) + "\n")
    print(f"wrote {out} ({len(body)} lines) from plan lines {opening + 2}-{closing}")


def cmd_tick(task: str, numbers: set[str]) -> None:
    lines = _read()
    start, end = _task_range(lines, task)
    done = 0
    for i in range(start, end):
        match = re.match(r"- \[ \] \*\*Step (\d+)", lines[i])
        if match and (not numbers or match.group(1) in numbers):
            lines[i] = "- [x]" + lines[i][5:]
            done += 1
    io.open(PLAN, "w", encoding="utf-8", newline="").write("\n".join(lines))
    print(f"ticked {done} step(s) in {task}")


# ---------------- 变异检查 ----------------
def cmd_mutate(source: str, old: str, new: str, pytest_args: list[str]) -> None:
    path = BACKEND / source
    backup = path.with_suffix(path.suffix + ".mutkeep")
    shutil.copyfile(path, backup)
    try:
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            sys.exit(f"变异位置应当恰好出现一次，实际 {text.count(old)} 次：{old}")
        path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
        run = subprocess.run([sys.executable, "-m", "pytest", *pytest_args], cwd=str(BACKEND),
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
    finally:
        shutil.copyfile(backup, path)
        backup.unlink()
    for line in run.stdout.splitlines():
        if line.startswith(("FAILED", "ERROR")):
            print(line)
    summary = [ln for ln in run.stdout.splitlines() if " passed" in ln or " failed" in ln]
    print(summary[-1] if summary else f"pytest 退出码 {run.returncode}")


def main(argv: list[str]) -> None:
    command, args = argv[1], argv[2:]
    if command == "where" and len(args) == 1:
        cmd_where(*args)
    elif command == "extract" and len(args) in (3, 4):
        cmd_extract(*args)
    elif command == "tick" and args:
        cmd_tick(args[0], set(args[1:]))
    elif command == "mutate" and len(args) >= 4:
        cmd_mutate(args[0], args[1], args[2], args[3:])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
