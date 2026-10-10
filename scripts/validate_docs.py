"""Catch broken canonical links and unterminated Markdown code fences."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    failures = []
    for path in [*ROOT.glob("*.md"), *ROOT.glob("docs/**/*.md")]:
        content = path.read_text(encoding="utf-8")
        if sum(bool(re.match(r"^\s*```", line)) for line in content.splitlines()) % 2:
            failures.append(f"{path.relative_to(ROOT)}: unterminated fence")
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", content):
            target = target.strip().strip("<>").split("#", 1)[0]
            if not target:
                continue
            if re.match(r"[a-zA-Z]+://", target):
                failures.append(f"{path.relative_to(ROOT)}: external Markdown link")
            elif not (path.parent / target).exists():
                failures.append(f"{path.relative_to(ROOT)}: missing link {target}")
    print("\n".join(failures) if failures else "Canonical documentation: PASS")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
