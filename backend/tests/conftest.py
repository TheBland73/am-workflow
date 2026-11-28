"""测试环境：把数据目录/数据库指向临时目录，必须在导入 app 之前完成。"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]

# 注意：本机沙箱下 tempfile.gettempdir() 会退化为 cwd，因此显式把测试临时目录
# 放到仓库内的 .pytest-tmp/（已 gitignore），并在进程退出时清理，避免污染 backend/。
_TMP_ROOT = BACKEND_DIR / ".pytest-tmp"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
for stale in _TMP_ROOT.glob("am-workflow-test-*"):
    shutil.rmtree(stale, ignore_errors=True)
_TMP = Path(tempfile.mkdtemp(prefix="am-workflow-test-", dir=_TMP_ROOT))
atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))


def pytest_sessionfinish(session, exitstatus):
    """测试结束先释放 SQLite 连接，再删临时目录（Windows 上文件被占用会删不掉）。"""
    try:
        from app.db.base import engine

        engine.dispose()
    except Exception:
        pass
    shutil.rmtree(_TMP, ignore_errors=True)

(_TMP / "data").mkdir(parents=True, exist_ok=True)
(_TMP / "runs").mkdir(parents=True, exist_ok=True)
os.environ.setdefault("AM_DATA_DIR", str(_TMP / "data"))
os.environ.setdefault("AM_RUNS_DIR", str(_TMP / "runs"))
os.environ.setdefault("AM_DATABASE_URL", f"sqlite:///{(_TMP / 'data' / 'test.db').as_posix()}")
os.environ.setdefault("AM_SECRET_KEY", "test-secret-key-0123456789abcdef0123456789ab")

sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture(autouse=True)
def _no_orchestrator_recovery(monkeypatch) -> None:
    """测试共用一个临时库：前面用例留下的运行，不该被后面用例启动应用时的恢复流程拉起来。"""
    from app.engine.orchestrator import orchestrator

    async def no_recover() -> None:
        return None

    monkeypatch.setattr(orchestrator, "recover", no_recover)
