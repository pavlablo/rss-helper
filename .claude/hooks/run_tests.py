"""PostToolUse-хук: после правки watcher.py или tests/ прогоняет тесты.
Если тесты упали — exit 2, и Claude видит ошибку и чинит её.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

sys.stderr.reconfigure(encoding="utf-8")
data = json.load(sys.stdin)
path = (data.get("tool_input") or {}).get("file_path") or ""
try:
    rel = Path(path).resolve().relative_to(ROOT).as_posix()
except ValueError:
    sys.exit(0)

if not (rel == "watcher.py" or (rel.startswith("tests/") and rel.endswith(".py"))):
    sys.exit(0)

env = dict(os.environ, PYTHONIOENCODING="utf-8")
r = subprocess.run(
    [sys.executable, "-m", "unittest", "discover", "tests", "-q"],
    cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
)
if r.returncode != 0:
    print("Тесты упали после правки " + rel + ":\n" + r.stderr[-4000:], file=sys.stderr)
    sys.exit(2)
