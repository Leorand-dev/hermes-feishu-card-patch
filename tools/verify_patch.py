#!/usr/bin/env python3
"""Integrity / state verifier for the Hermes Feishu streaming-card patch.

Modes:
  (default)                verify the shipped patch + manifest hashes/hunk count
  --check-dir DIR          verify an INSTALLED Hermes checkout (patched hashes)
  --check-pristine DIR     verify a PRISTINE Hermes checkout (original hashes)
  --print-rollback DIR     print the rollback command for an installed checkout

Exit code 0 = OK, 1 = mismatch, 2 = missing artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_VERSION = "ca069e1223"


def sha256_file(path: str):
    if not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(version: str):
    base = os.path.join(REPO_DIR, "patches", version)
    manifest_path = os.path.join(base, "MANIFEST.json")
    patch_path = os.path.join(base, "hermes-feishu-card.patch")
    if not os.path.isfile(manifest_path) or not os.path.isfile(patch_path):
        print(f"missing artifacts under {base}")
        raise SystemExit(2)
    with open(manifest_path, "r", encoding="utf-8") as fh:
        return json.load(fh), patch_path


def main() -> int:
    ap = argparse.ArgumentParser(description="verify the Hermes Feishu streaming-card patch")
    ap.add_argument("--version", default=DEFAULT_VERSION)
    ap.add_argument("--check-dir", default="", help="installed Hermes checkout to verify")
    ap.add_argument("--check-pristine", default="", help="pristine Hermes checkout to verify")
    args = ap.parse_args()

    manifest, patch_path = load(args.version)
    failures = 0

    print(f"patch build: {args.version}")
    print(f"  upstream commit: {manifest['upstream_commit']}")
    print(f"  files:           {len(manifest['files'])}")
    actual_patch_sha = sha256_file(patch_path)
    ok = actual_patch_sha == manifest["patch_sha256"]
    print(f"  patch sha256:    {'OK' if ok else 'MISMATCH'} ({actual_patch_sha})")
    failures += 0 if ok else 1
    with open(patch_path, "r", encoding="utf-8") as fh:
        hunks = fh.read().count("\n@@ ")
    ok = hunks == manifest["hunks"]
    print(f"  hunks:           {'OK' if ok else 'MISMATCH'} ({hunks}/{manifest['hunks']})")
    failures += 0 if ok else 1

    if args.check_dir:
        print(f"installed checkout: {args.check_dir}")
        dir_failures = 0
        for rel, info in manifest["files"].items():
            actual = sha256_file(os.path.join(args.check_dir, rel))
            ok = actual == info["patched_sha256"]
            if not ok:
                dir_failures += 1
                print(f"  MISMATCH {rel}: {actual}")
        if dir_failures == 0:
            print(f"  all {len(manifest['files'])} patched hashes match")
        else:
            failures += dir_failures

    if args.check_pristine:
        print(f"pristine checkout: {args.check_pristine}")
        for rel, info in manifest["files"].items():
            path = os.path.join(args.check_pristine, rel)
            if info.get("new_file"):
                if os.path.isfile(path):
                    failures += 1
                    print(f"  UNEXPECTED new file present: {rel}")
                continue
            actual = sha256_file(path)
            ok = actual == info["original_sha256"]
            if not ok:
                failures += 1
                print(f"  MISMATCH {rel}: {actual}")

    print("RESULT:", "OK" if failures == 0 else f"FAILED ({failures} problem(s))")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
