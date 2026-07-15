#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 - "$root" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
missing: list[str] = []
pattern = re.compile(r"\[[^]]+\]\(([^)]+)\)")
for document in sorted(root.rglob("*.md")):
    if any(part.startswith(".") and part not in {"."} for part in document.relative_to(root).parts):
        continue
    for target in pattern.findall(document.read_text(encoding="utf-8")):
        target = target.strip().strip("<>").partition("#")[0]
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        resolved = (document.parent / target).resolve()
        if not resolved.exists():
            missing.append(f"{document.relative_to(root)} -> {target}")
if missing:
    raise SystemExit("Missing documentation links:\n" + "\n".join(missing))
print("documentation links: ok")
PY
