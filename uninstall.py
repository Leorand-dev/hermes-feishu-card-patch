#!/usr/bin/env python3
"""Rollback for the Hermes Feishu streaming-card patch.

Restores the target files (and config.yaml) captured by ``install.sh`` in
``~/.hermes/backups/pre-hermes-feishu-card-*``, removes the new card_stream.py
module, verifies the restored hashes and (optionally) restarts the gateway.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_VERSION = "ca069e1223"
DEFAULT_HERMES_DIR = os.path.expanduser("~/.hermes/hermes-agent")


def sha256_file(path: str):
    if not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(version: str) -> dict:
    path = os.path.join(REPO_DIR, "patches", version, "MANIFEST.json")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def latest_backup() -> str:
    candidates = sorted(glob.glob(os.path.expanduser("~/.hermes/backups/pre-hermes-feishu-card-*")))
    if not candidates:
        raise SystemExit("no pre-hermes-feishu-card-* backup found; pass --backup <dir>")
    return candidates[-1]


def refresh_cache(hermes_dir: str) -> None:
    """Drop stale .pyc so the next start cannot import the old bytecode."""
    removed = 0
    for root, dirs, files in os.walk(os.path.join(hermes_dir, "gateway")):
        if "__pycache__" not in dirs:
            continue
        pkg = os.path.join(root, "__pycache__")
        for name in list(os.listdir(pkg)):
            if name.startswith(("stream_consumer", "run_turn", "runtime_footer", "base.")):
                try:
                    os.remove(os.path.join(pkg, name))
                    removed += 1
                except OSError:
                    pass
    feishu_pyc = os.path.join(hermes_dir, "plugins", "platforms", "feishu", "__pycache__")
    if os.path.isdir(feishu_pyc):
        for name in list(os.listdir(feishu_pyc)):
            if name.startswith(("adapter", "card_stream")):
                try:
                    os.remove(os.path.join(feishu_pyc, name))
                    removed += 1
                except OSError:
                    pass
    print(f"  cleared {removed} cached bytecode file(s)")


def restart_gateway() -> bool:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        print("  restart: systemctl not found — restart the gateway manually")
        return False
    state = subprocess.run([systemctl, "--user", "is-active", "hermes-gateway"],
                           capture_output=True, text=True).stdout.strip()
    if state not in ("active", "activating"):
        print(f"  restart: hermes-gateway is '{state or 'unknown'}' — start it manually")
        return False
    proc = subprocess.run([systemctl, "--user", "restart", "hermes-gateway"], capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"  restart FAILED: {proc.stderr.strip()}")
        return False
    import time
    for _ in range(20):
        time.sleep(0.5)
        if subprocess.run([systemctl, "--user", "is-active", "hermes-gateway"],
                          capture_output=True, text=True).stdout.strip() == "active":
            print("  restart: hermes-gateway is active again")
            return True
    print("  restart: service did not report active within 10s")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(prog="uninstall.sh",
                                 description="Roll back the Hermes Feishu streaming-card patch.")
    ap.add_argument("--hermes-dir", default=os.environ.get("HERMES_DIR", DEFAULT_HERMES_DIR))
    ap.add_argument("--version", default=DEFAULT_VERSION)
    ap.add_argument("--backup", default="", help="backup dir (default: newest)")
    ap.add_argument("--config", default="", help="config.yaml to restore (default: backup copy)")
    ap.add_argument("--to-pristine", action="store_true",
                    help="restore the pinned upstream files from git HEAD instead of the pre-install backup "
                         "(removes the patch entirely; use after several re-installs)")
    ap.add_argument("--no-restart", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    hermes_dir = os.path.abspath(os.path.expanduser(args.hermes_dir))
    backup = os.path.abspath(os.path.expanduser(args.backup)) if args.backup else latest_backup()
    manifest = load_manifest(args.version)
    print(f"rollback hermes-feishu-card {args.version}")
    print(f"  checkout: {hermes_dir}")
    print(f"  backup:   {backup}")
    if not os.path.isdir(backup) and not args.to_pristine:
        raise SystemExit(f"backup dir not found: {backup}")

    plan = []
    if args.to_pristine:
        for rel, info in manifest["files"].items():
            dst = os.path.join(hermes_dir, rel)
            plan.append(("remove", dst) if info.get("new_file") else ("restore-head", dst))
    else:
        for rel, info in manifest["files"].items():
            src = os.path.join(backup, "files", rel)
            dst = os.path.join(hermes_dir, rel)
            if info.get("new_file"):
                plan.append(("remove", dst))
            elif os.path.isfile(src):
                plan.append(("restore", dst))
            else:
                print(f"  warning: backup has no copy of {rel}; leaving it alone")
    config_dst = os.path.join(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")), "config.yaml")
    config_src = os.path.join(backup, "config.yaml")
    if args.config:
        config_dst = os.path.abspath(os.path.expanduser(args.config))
    if os.path.isfile(config_src):
        plan.append(("restore-config", config_dst))

    if args.dry_run:
        for action, path in plan:
            print(f"  would {action}: {path}")
        return 0

    if args.to_pristine:
        rels = [rel for rel, info in manifest["files"].items() if not info.get("new_file")]
        git = shutil.which("git")
        if not git:
            raise SystemExit("git not found — required for --to-pristine")
        proc = subprocess.run(
            [git, "-C", hermes_dir, "restore", "--source=HEAD", "--staged", "--worktree", "--", *rels],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise SystemExit(f"git restore to HEAD failed: {proc.stderr.strip()}")
        print(f"  restored {len(rels)} files from git HEAD")

    for action, dst in plan:
        if action == "remove":
            if os.path.isfile(dst):
                os.remove(dst)
                print(f"  removed {dst}")
        elif action == "restore":
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(backup, "files", os.path.relpath(dst, hermes_dir)), dst)
            print(f"  restored {dst}")
        elif action == "restore-config":
            shutil.copy2(config_src, config_dst)
            print(f"  restored {config_dst}")

    bad = []
    restored_to_pristine = True
    if args.to_pristine:
        for rel, info in manifest["files"].items():
            dst = os.path.join(hermes_dir, rel)
            if info.get("new_file"):
                if os.path.isfile(dst):
                    bad.append(rel + " (new file still present)")
                continue
            if sha256_file(dst) != info["original_sha256"]:
                bad.append(rel + " (does not match the pinned build)")
        if bad:
            raise SystemExit("rollback to pristine failed:\n" + "\n".join(f"  - {rel}" for rel in bad))
        print(f"  verified pristine hashes: {len(manifest['files'])} files")
    else:
        for rel, info in manifest["files"].items():
            dst = os.path.join(hermes_dir, rel)
            if info.get("new_file"):
                if os.path.isfile(dst):
                    bad.append(rel + " (new file still present)")
                continue
            backup_copy = os.path.join(backup, "files", rel)
            expected = sha256_file(backup_copy) if os.path.isfile(backup_copy) else None
            actual = sha256_file(dst)
            if expected is not None and actual != expected:
                bad.append(rel + " (does not match the pre-install backup)")
            elif expected is None and actual is not None:
                bad.append(rel + " (no backup copy to verify against)")
            if expected is not None and expected != info["original_sha256"]:
                restored_to_pristine = False
        if bad:
            raise SystemExit("rollback hash verification failed:\n" + "\n".join(f"  - {rel}" for rel in bad))
        if restored_to_pristine:
            print(f"  verified pristine hashes: {len(manifest['files'])} files")
        else:
            print("  restored the pre-install state (backup contained local modifications; "
                  "pristine-hash check skipped by design)")
            print("  to force-verify against the pinned pristine build, compare with "
                  "./tools/verify_patch.py --check-pristine <dir>")
    refresh_cache(hermes_dir)
    if not args.no_restart:
        restart_gateway()
    print("\nRolled back OK. Original files restored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
