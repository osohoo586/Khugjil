"""Per-package coverage gate (coverage.py only supports one global threshold).

SPEC §11 requires >= 95% on the parser, filters, routing, consent, on-duty logic and tenant
isolation. Each gated path below must exist once its feature lands; missing paths are reported
as "not yet implemented" rather than silently passing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

THRESHOLD = 95.0
GATED = [
    "khugjil/ingest/parsers/",
    "khugjil/ingest/checks.py",
    "khugjil/ingest/denylist.py",
    "khugjil/ingest/sanitize.py",
    "khugjil/ingest/mime.py",
    "khugjil/ingest/dkim_arc.py",
    "khugjil/routing/",
    "khugjil/db/tenancy.py",
]


def main(path: str) -> int:
    data = json.loads(Path(path).read_text())
    files = data["files"]
    failed = False
    for prefix in GATED:
        covered = total = 0
        for name, info in files.items():
            norm = name.replace("\\", "/")
            if f"/{prefix}" in f"/{norm}" or norm.endswith(prefix):
                s = info["summary"]
                covered += s["covered_lines"] + s.get("covered_branches", 0)
                total += s["num_statements"] + s.get("num_branches", 0)
        if total == 0:
            print(f"  {prefix:<36} not yet implemented")
            continue
        pct = 100.0 * covered / total
        mark = "ok  " if pct >= THRESHOLD else "FAIL"
        failed |= pct < THRESHOLD
        print(f"{mark} {prefix:<36} {pct:6.2f}%")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
