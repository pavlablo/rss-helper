"""PreToolUse-хук: не даёт Claude редактировать seen.json.
Этот файл ведёт бот в GitHub Actions; ручные правки ломают git pull и состояние.
"""
import json
import sys
from pathlib import Path

sys.stderr.reconfigure(encoding="utf-8")
data = json.load(sys.stdin)
path = (data.get("tool_input") or {}).get("file_path") or ""
if Path(path).name == "seen.json":
    print("seen.json ведёт бот в GitHub Actions — не редактируй его вручную.", file=sys.stderr)
    sys.exit(2)
