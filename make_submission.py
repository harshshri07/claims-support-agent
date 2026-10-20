"""Build the submission zip: source only, no secrets, no local environments.

    python make_submission.py            ->  dist/sop-guided-conversational-agent.zip

Same layout as the provided starter zip (apps/insurance_claims/fixtures/... at the root), plus the solution.

Refuses to build if any packaged file contains a value from your local .env or something that looks like a key.
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "sop-guided-conversational-agent"
EXCLUDE_DIRS = {".venv", "__pycache__", ".pytest_cache", ".git", "dist", ".idea", ".vscode"}
EXCLUDE_FILES = {".env", "last_report.json"}
EXCLUDE_SUFFIX = {".zip", ".pyc"}
KEY_PATTERNS = [re.compile(p) for p in (r"AKIA[0-9A-Z]{16}", r"sk-[A-Za-z0-9_-]{20,}", r"sk-ant-[A-Za-z0-9_-]{20,}",
                                         r"AIza[0-9A-Za-z_-]{30,}")]


def local_secrets() -> list[str]:
    env = ROOT / ".env"
    if not env.exists():
        return []
    vals = []
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            v = line.split("=", 1)[1].strip().strip('"\'')
            if len(v) >= 12 and any(k in line.upper() for k in ("KEY", "SECRET", "TOKEN", "PASSWORD")):
                vals.append(v)
    return vals


def files() -> list[Path]:
    out = []
    for p in sorted(ROOT.rglob("*")):
        rel = p.relative_to(ROOT)
        if p.is_dir() or any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if p.name in EXCLUDE_FILES or p.suffix in EXCLUDE_SUFFIX:
            continue
        out.append(p)
    return out


def main() -> None:
    secrets = local_secrets()
    picked = files()
    problems = []
    for p in picked:
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(s in text for s in secrets) or (p.name != "make_submission.py" and any(r.search(text) for r in KEY_PATTERNS)):
            problems.append(str(p.relative_to(ROOT)))
    if problems:
        sys.exit("Refusing to build: possible secrets in " + ", ".join(problems))
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    target = dist / f"{NAME}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for p in picked:
            z.write(p, p.relative_to(ROOT).as_posix())
    print(f"{target}  ({len(picked)} files, {target.stat().st_size // 1024} KB, secret scan clean)")


if __name__ == "__main__":
    main()
