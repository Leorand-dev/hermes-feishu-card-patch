"""Build the installable unified patch from ``pristine/`` + ``work/`` overlays.

Dev-time helper (run from the patch repo root):

    python tools/make_patch.py --version ca069e1223

It writes ``patches/<version>/hermes-feishu-card.patch`` plus
``patches/<version>/MANIFEST.json`` (per-file pristine/patched SHA-256 + hunk
counts).  The installer consumes only these two artifacts.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(REPO, "work")
PRISTINE = os.path.join(REPO, "pristine")

# Files touched by the patch (path relative to the Hermes checkout root).
TARGETS = [
    "gateway/platforms/base.py",
    "gateway/run_turn.py",
    "gateway/run_turn_persistence.py",
    "gateway/run_turn_runner.py",
    "gateway/run_turn_progress.py",
    "gateway/runtime_footer.py",
    "gateway/stream_consumer.py",
    "plugins/platforms/feishu/adapter.py",
]
NEW_FILES = [
    "plugins/platforms/feishu/card_stream.py",
    "plugins/platforms/feishu/cardkit_v2.py",
]


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def build_patch() -> tuple[str, list[str]]:
    chunks: list[str] = []
    touched: list[str] = []
    for rel in TARGETS:
        old_path = os.path.join(PRISTINE, rel)
        new_path = os.path.join(WORK, rel)
        if not os.path.isfile(old_path):
            raise SystemExit(f"missing pristine copy: {old_path}")
        if not os.path.isfile(new_path):
            raise SystemExit(f"missing patched copy: {new_path}")
        old = read_text(old_path).splitlines(keepends=True)
        new = read_text(new_path).splitlines(keepends=True)
        if old == new:
            continue
        diff = list(difflib.unified_diff(
            old, new, fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3,
        ))
        if not diff:
            continue
        chunks.extend(diff)
        chunks.append("")
        touched.append(rel)
    for rel in NEW_FILES:
        new_path = os.path.join(WORK, rel)
        if not os.path.isfile(new_path):
            raise SystemExit(f"missing new file: {new_path}")
        new = read_text(new_path).splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            [], new, fromfile="/dev/null", tofile=f"b/{rel}", n=3,
        ))
        chunks.extend(diff)
        chunks.append("")
        touched.append(rel)
    return "".join(chunks), touched


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="ca069e1223")
    ap.add_argument("--upstream-commit", default="ca069e12239a2c8d13887ac9f983557d00bdcfd2")
    args = ap.parse_args()

    patch, touched = build_patch()
    out_dir = os.path.join(REPO, "patches", args.version)
    os.makedirs(out_dir, exist_ok=True)
    patch_path = os.path.join(out_dir, "hermes-feishu-card.patch")
    with open(patch_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(patch)

    targets = {}
    for rel in TARGETS + NEW_FILES:
        pristine_path = os.path.join(PRISTINE, rel)
        patched_path = os.path.join(WORK, rel)
        entry = {
            "path": rel,
            "original_sha256": sha256(pristine_path) if os.path.isfile(pristine_path) else None,
            "patched_sha256": sha256(patched_path),
            "new_file": rel in NEW_FILES,
        }
        targets[rel] = entry

    manifest = {
        "manifest_version": 1,
        "patch_name": "hermes-feishu-card",
        "description": "Feishu streaming-card presentation for Hermes Agent "
                       "(one bounded card per reply, tool/narration status, footer note)",
        "upstream_repo": "https://github.com/NousResearch/hermes-agent",
        "upstream_commit": args.upstream_commit,
        "patch_file": "hermes-feishu-card.patch",
        "patch_sha256": sha256(patch_path),
        "hunks": patch.count("\n@@ "),
        "files": targets,
    }
    manifest_path = os.path.join(out_dir, "MANIFEST.json")
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"patch:    {patch_path} ({len(patch)} bytes, {manifest['hunks']} hunks)")
    print(f"manifest: {manifest_path}")
    for rel in touched:
        print(f"  {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
