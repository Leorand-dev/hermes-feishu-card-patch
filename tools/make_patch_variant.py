"""Build an additional exact-build patch for a DIFFERENT upstream commit.

Dev-time helper (mirrors tools/make_patch.py, but takes explicit pristine/patched
overlays instead of the repository's pinned ca069e1223 overlay):

    python tools/make_patch_variant.py \
        --version ccc6956 \
        --upstream-commit ccc695635094d7615373db267b77f894d978b1a0 \
        --pristine-dir D:/.../v89/pristine --patched-dir D:/.../v89/patched

Used for older target hosts whose Hermes build is far ahead of
the pinned ca069e1223; its adapter/gateway files were rebased once and are otherwise
identical to this repository's transport implementation.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--upstream-commit", required=True)
    ap.add_argument("--pristine-dir", required=True)
    ap.add_argument("--patched-dir", required=True)
    ap.add_argument("--patch-name", default="hermes-feishu-card")
    args = ap.parse_args()

    pristine = os.path.abspath(args.pristine_dir)
    patched = os.path.abspath(args.patched_dir)
    chunks: list[str] = []
    touched: list[str] = []
    for rel in TARGETS:
        old = read_text(os.path.join(pristine, rel)).splitlines(keepends=True)
        new = read_text(os.path.join(patched, rel)).splitlines(keepends=True)
        if old == new:
            continue
        chunks.extend(difflib.unified_diff(old, new, fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3))
        chunks.append("")
        touched.append(rel)
    for rel in NEW_FILES:
        new = read_text(os.path.join(patched, rel)).splitlines(keepends=True)
        chunks.extend(difflib.unified_diff([], new, fromfile="/dev/null", tofile=f"b/{rel}", n=3))
        chunks.append("")
        touched.append(rel)
    patch_text = "".join(chunks)

    out_dir = os.path.join(REPO, "patches", args.version)
    os.makedirs(out_dir, exist_ok=True)
    patch_path = os.path.join(out_dir, f"{args.patch_name}.patch")
    with open(patch_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(patch_text)

    files = {}
    for rel in TARGETS + NEW_FILES:
        orig_path = os.path.join(pristine, rel)
        files[rel] = {
            "path": rel,
            "original_sha256": sha256(orig_path) if os.path.isfile(orig_path) else None,
            "patched_sha256": sha256(os.path.join(patched, rel)),
            "new_file": rel in NEW_FILES,
        }
    manifest = {
        "manifest_version": 1,
        "patch_name": args.patch_name,
        "description": "Feishu streaming-card presentation for Hermes Agent "
                       "(one bounded card per reply, tool/narration status, footer note)",
        "upstream_repo": "https://github.com/NousResearch/hermes-agent",
        "upstream_commit": args.upstream_commit,
        "patch_file": f"{args.patch_name}.patch",
        "patch_sha256": sha256(patch_path),
        "hunks": patch_text.count("\n@@ "),
        "files": files,
    }
    with open(os.path.join(out_dir, "MANIFEST.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"variant build {args.version}: {len(touched)} files, "
          f"{manifest['hunks']} hunks, patch {patch_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
