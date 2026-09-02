#!/usr/bin/env python3
"""Set the version in backend/pyproject.toml.

This was a `sed -i '' "0,/^version = .../s//.../"` in the Makefile. That form
is GNU-only: BSD sed accepts the line-0 address, matches nothing, and exits 0.
On macOS it was a silent no-op, and the first release this repo ever cut would
have tagged a version the file did not claim. A file you can run is a file you
can test; the one-liner was neither.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "backend" / "pyproject.toml"
VERSION_RE = re.compile(r'^version = "(?P<version>[^"]*)"$', re.MULTILINE)
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def bump(text: str, new: str) -> tuple[str, str]:
    """Return (rewritten text, the version it replaced)."""
    matches = list(VERSION_RE.finditer(text))
    if not matches:
        raise SystemExit(f"no `version = \"...\"` line in {PYPROJECT}")
    if len(matches) > 1:
        # Two version lines means the file grew a shape this cannot reason
        # about. Refusing beats picking one and being right by luck.
        lines = ", ".join(str(text[: m.start()].count("\n") + 1) for m in matches)
        raise SystemExit(f"{PYPROJECT} has {len(matches)} version lines (at {lines})")
    match = matches[0]
    return (
        text[: match.start()] + f'version = "{new}"' + text[match.end() :],
        match.group("version"),
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("usage: bump_version.py <version>   (no leading v)")
    new = argv[1]
    if not SEMVER_RE.match(new):
        raise SystemExit(f"{new!r} is not a semver version like 1.2.3")

    text = PYPROJECT.read_text()
    rewritten, old = bump(text, new)
    PYPROJECT.write_text(rewritten)

    # Read it back. The whole reason this file exists is that the previous
    # implementation reported success without changing anything.
    confirmed = VERSION_RE.search(PYPROJECT.read_text())
    if confirmed is None or confirmed.group("version") != new:
        raise SystemExit(f"wrote {new} but the file reads back as {confirmed and confirmed.group('version')!r}")

    print(f"  {old} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
