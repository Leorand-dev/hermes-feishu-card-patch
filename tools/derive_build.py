#!/usr/bin/env python3
"""Derive a new exact-build patch from an upstream Hermes checkout.

This automates the mechanical part of a Hermes upgrade:

1. create a clean worktree at the target upstream commit;
2. apply the current Feishu card patch (``--patch``, default: the current
   bf0203a7 build) with fuzz;
3. collect the resulting patched files + their pristine versions;
4. call ``tools/make_patch_variant.py`` to emit ``patches/<version>/``.

Conflicts are reported with ``.rej`` paths and a non-zero exit code; resolve
those hunks in the printed worktree (``--keep``), then re-run with
``--from-worktree`` to build from it.

Examples:
    python tools/derive_build.py --commit <sha> --version <name>
    python tools/derive_build.py --commit <sha> --version <name> --dry-run
    python tools/derive_build.py --from-worktree /tmp/hfcp-derive-xxx/wt --version <name>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATCH = os.path.join(REPO, "patches", "bf0203a7", "hermes-feishu-card.patch")
TARGETS_FALLBACK = [
    "gateway/platforms/base.py",
    "gateway/run_turn.py",
    "gateway/run_turn_persistence.py",
    "gateway/run_turn_runner.py",
    "gateway/run_turn_progress.py",
    "gateway/runtime_footer.py",
    "gateway/stream_consumer.py",
    "plugins/platforms/feishu/adapter.py",
    "plugins/platforms/feishu/card_stream.py",
]


def run(cmd: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


def git_show(checkout: str, commit: str, rel: str) -> str | None:
    proc = run(["git", "-C", checkout, "show", f"{commit}:{rel}"], check=False)
    return proc.stdout if proc.returncode == 0 else None


def target_paths(patch_file: str) -> list[str]:
    paths: list[str] = []
    with open(patch_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("+++ b/"):
                rel = line[6:].strip()
                if rel and rel not in paths and rel != "/dev/null":
                    paths.append(rel)
    return paths or list(TARGETS_FALLBACK)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkout", default=os.path.join(os.path.expanduser("~"), ".hermes", "hermes-agent"))
    ap.add_argument("--commit", default="HEAD")
    ap.add_argument("--version", default="")
    ap.add_argument("--patch", default=DEFAULT_PATCH)
    ap.add_argument("--workdir", default="")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--from-worktree", default="")
    args = ap.parse_args()

    if not os.path.isdir(args.checkout):
        print(f"checkout not found: {args.checkout}")
        return 2
    if not args.from_worktree and not os.path.isfile(args.patch):
        print(f"patch not found: {args.patch}")
        return 2

    cache_root = os.path.join(os.path.expanduser("~"), ".cache")
    os.makedirs(cache_root, exist_ok=True)
    workdir = args.workdir or tempfile.mkdtemp(prefix="hfcp-derive-", dir=cache_root)
    os.makedirs(workdir, exist_ok=True)
    wt = args.from_worktree or os.path.join(workdir, "wt")
    pristine = os.path.join(workdir, "pristine")
    patched = os.path.join(workdir, "patched")
    created_wt = False
    try:
        if not args.from_worktree:
            commit = run(["git", "-C", args.checkout, "rev-parse", args.commit]).stdout.strip()
            print(f"target upstream: {commit[:12]}  ({args.checkout})")
            if os.path.isdir(wt):
                shutil.rmtree(wt)
            run(["git", "-C", args.checkout, "worktree", "add", "--detach", wt, commit])
            created_wt = True
            targets = target_paths(args.patch)
            patch_proc = run(
                ["patch", "-p1", "--forward", "--fuzz=3", "--batch", "-i", os.path.abspath(args.patch)],
                cwd=wt, check=False,
            )
            if patch_proc.stdout:
                print(patch_proc.stdout[-2000:])
            if patch_proc.stderr:
                print(patch_proc.stderr[-2000:], file=sys.stderr)
            rejects = []
            for root, _dirs, files in os.walk(wt):
                rejects.extend(os.path.join(root, f) for f in files if f.endswith(".rej"))
            if rejects:
                print("\nCONFLICTS — resolve these .rej files, then re-run with --keep / --from-worktree:")
                for path in rejects:
                    print("  " + path)
                if not args.keep:
                    print(f"\nworktree kept for resolution: {wt}")
                    created_wt = False
                return 3
            print("patch applied cleanly")
        else:
            targets = target_paths(args.patch) if os.path.isfile(args.patch) else list(TARGETS_FALLBACK)
            if args.dry_run:
                print("worktree supplied; not building with --dry-run")
                return 0

        if args.dry_run:
            print("dry-run OK (patch applies); no build emitted")
            return 0

        # Materialize pristine/patched target-only overlays.
        for directory in (pristine, patched):
            if os.path.isdir(directory):
                shutil.rmtree(directory)
        for rel in targets:
            src = os.path.join(wt, rel)
            if not os.path.isfile(src):
                print(f"missing patched file after apply: {rel}")
                return 4
            dst = os.path.join(patched, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            original = None if args.from_worktree and not os.path.isfile(os.path.join(wt, rel)) else git_show(
                args.checkout, args.commit if args.commit != "HEAD" else "HEAD", rel)
            if original is not None and args.commit:
                dst_orig = os.path.join(pristine, rel)
                os.makedirs(os.path.dirname(dst_orig), exist_ok=True)
                with open(dst_orig, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(original)
        if not os.path.isdir(pristine):
            print("could not materialize pristine files (git show failed?)")
            return 4

        version = args.version or (run(["git", "-C", args.checkout, "rev-parse", "--short=8", "HEAD"]).stdout.strip()
                                   + "-derived")
        upstream_commit = run(["git", "-C", args.checkout, "rev-parse", args.commit]).stdout.strip()
        build = run([
            sys.executable, os.path.join(REPO, "tools", "make_patch_variant.py"),
            "--version", version,
            "--upstream-commit", upstream_commit,
            "--pristine-dir", pristine,
            "--patched-dir", patched,
        ])
        print(build.stdout.strip())
        print(f"\nbuilt patches/{version}/ — verify it with:")
        print(f"  python tools/verify_patch.py --version {version}")
        print("Then deploy with: install.sh --version " + version)
        return 0
    finally:
        if not args.from_worktree and created_wt and not args.keep:
            subprocess.run(["git", "-C", args.checkout, "worktree", "remove", "--force", wt],
                           check=False, capture_output=True)
            shutil.rmtree(workdir, ignore_errors=True)
        elif args.keep and not args.from_worktree:
            print(f"kept workdir: {workdir} (worktree: {wt})")


if __name__ == "__main__":
    sys.exit(main())
