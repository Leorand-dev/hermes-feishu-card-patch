#!/usr/bin/env python3
"""Regenerate SHA256SUMS for the repository (excludes .git/, __pycache__, itself)."""
import hashlib
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXCLUDE_DIRS = {".git", "__pycache__"}
EXCLUDE_FILES = {"SHA256SUMS"}


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    entries = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS)
        for name in sorted(files):
            if name in EXCLUDE_FILES:
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, REPO).replace(os.sep, "/")
            entries.append((rel, sha256(full)))
    with open(os.path.join(REPO, "SHA256SUMS"), "w", encoding="utf-8", newline="\n") as fh:
        for rel, digest in entries:
            fh.write(f"{digest}  {rel}\n")
    print(f"wrote SHA256SUMS ({len(entries)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
