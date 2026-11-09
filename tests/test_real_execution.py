"""Opt-in execution against installed AstrBot, isolated from contract-test imports."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CASES = ("search", "generate", "generate_nulls", "generate_legacy", "edit", "gif", "persona_permissions")


@pytest.mark.skipif(
    os.environ.get("OPENAI_NATIVE_REAL_ASTRBOT") != "1",
    reason="Set OPENAI_NATIVE_REAL_ASTRBOT=1 with real AstrBot on PYTHONPATH",
)
@pytest.mark.parametrize("case", CASES)
def test_real_astrbot_executor_with_mock_http(case):
    # Framework imports initialize files and global registries. Keep them in a
    # fresh interpreter, with all runtime/service/temp files under the session.
    runtime = Path(
        os.environ.get("OPENAI_NATIVE_ASTRBOT_RUNTIME", ROOT.parent / ".framework-validation" / "runtime")
    ).resolve()
    temporary = runtime / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"real-execution-{case}-", dir=temporary) as data_dir:
        env = {
            **os.environ,
            "ASTRBOT_ROOT": str(runtime),
            "TEMP": data_dir,
            "TMP": data_dir,
            "TMPDIR": data_dir,
            "XDG_CACHE_HOME": str(runtime / "cache"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "PYTHONPATH": os.pathsep.join(filter(None, (str(ROOT.parent), os.environ.get("PYTHONPATH")))),
        }
        completed = subprocess.run(
            [sys.executable, str(ROOT / "tests" / "real_execution_case.py"), case, data_dir],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            cwd=runtime,
            env=env,
        )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    summaries = [
        line.removeprefix("REAL_EXECUTION_OK ")
        for line in completed.stdout.splitlines()
        if line.startswith("REAL_EXECUTION_OK ")
    ]
    assert len(summaries) == 1, output
    assert json.loads(summaries[0]) == {"case": case, "posts": 1}
