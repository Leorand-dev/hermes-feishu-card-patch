#!/usr/bin/env python3
"""Exact-build installer for the Hermes Feishu streaming-card patch.

Installs the build-pinned unified patch shipped in ``patches/<version>/`` onto a
Hermes Agent checkout:

  1. verifies the upstream commit (HEAD) and every target file hash (pristine),
  2. backs up the target files (and a diff) to ~/.hermes/backups/,
  3. applies the unified patch (``patch -p1``; ``git apply`` fallback),
  4. verifies the patched hashes and byte-compiles every target,
  5. activates the card transport in the gateway config (``platforms.feishu.card_replies``
     + per-platform runtime footer) unless ``--skip-config`` is passed,
  6. optionally restarts the user gateway service and checks it comes back clean.

Refuses unknown builds / already-modified targets instead of fuzzy-patching.
Run ``./install.sh --help`` for flags.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
from typing import Any, Optional

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_VERSION = "ca069e1223"
DEFAULT_HERMES_DIR = os.path.expanduser("~/.hermes/hermes-agent")


# --------------------------------------------------------------------------- helpers

def sha256_file(path: str) -> Optional[str]:
    if not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd: list[str], *, cwd: Optional[str] = None, check: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=capture)
    if check and proc.returncode != 0:
        raise SystemExit(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return proc


def git_head(hermes_dir: str) -> str:
    proc = run(["git", "-C", hermes_dir, "rev-parse", "HEAD"])
    return proc.stdout.strip() if proc.returncode == 0 else ""


def git_dirty_targets(hermes_dir: str, paths: list[str]) -> list[str]:
    proc = run(["git", "-C", hermes_dir, "status", "--porcelain", "--", *paths])
    dirty = []
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        dirty.append(path)
    return dirty


def load_manifest(version: str) -> dict[str, Any]:
    path = os.path.join(REPO_DIR, "patches", version, "MANIFEST.json")
    if not os.path.isfile(path):
        raise SystemExit(f"manifest not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def resolve_python(hermes_dir: str) -> list[str]:
    for candidate in (os.path.join(hermes_dir, ".venv", "bin", "python"),
                      os.path.join(hermes_dir, ".venv", "bin", "python3")):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return [candidate]
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return [found]
    raise SystemExit("no python interpreter found (need python3)")


# --------------------------------------------------------------------------- config

def activate_config(config_path: str, *, enable_footer: bool, log=print) -> bool:
    """Set platforms.feishu.card_replies=true and enable the Feishu runtime footer."""
    if not os.path.isfile(config_path):
        log(f"  config: {config_path} not found — skipped (set platforms.feishu.card_replies: true manually)")
        return False
    try:
        from ruamel.yaml import YAML  # type: ignore
    except Exception:
        log("  config: ruamel.yaml unavailable — skipped (set platforms.feishu.card_replies: true manually)")
        return False
    yaml = YAML()
    yaml.preserve_quotes = True
    with open(config_path, "r", encoding="utf-8") as fh:
        cfg = yaml.load(fh)
    if cfg is None:
        log("  config: empty config — skipped")
        return False
    platforms = cfg.setdefault("platforms", {})
    feishu = platforms.setdefault("feishu", {})
    if not isinstance(feishu, dict):
        log("  config: platforms.feishu is not a mapping — skipped")
        return False
    changed = False
    if feishu.get("card_replies") is not True:
        feishu["card_replies"] = True
        changed = True
    if enable_footer:
        display = cfg.setdefault("display", {})
        dplatforms = display.setdefault("platforms", {})
        df = dplatforms.setdefault("feishu", {})
        footer = df.setdefault("runtime_footer", {})
        if footer.get("enabled") is not True:
            footer["enabled"] = True
            changed = True
        if not footer.get("fields"):
            footer["fields"] = ["model", "context_pct", "cache", "tok_s", "latency"]
            changed = True
        elif "latency" not in list(footer["fields"]):
            # v0.22.5 card rollout: append the turn wall-clock so long turns show duration.
            footer["fields"] = list(footer["fields"]) + ["latency"]
            changed = True
    if not changed:
        log("  config: already activated")
        return False
    with open(config_path, "w", encoding="utf-8") as fh:
        yaml.dump(cfg, fh)
    log(f"  config: activated card transport{' + Feishu runtime footer' if enable_footer else ''} in {config_path}")
    return True


# --------------------------------------------------------------------------- install steps

def inspect_state(hermes_dir: str, manifest: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """Return (pristine, patched, unknown) target lists."""
    pristine: list[str] = []
    patched: list[str] = []
    unknown: list[str] = []
    for rel, info in manifest["files"].items():
        path = os.path.join(hermes_dir, rel)
        actual = sha256_file(path)
        if info.get("new_file"):
            if actual is None:
                pristine.append(rel)
            elif actual == info["patched_sha256"]:
                patched.append(rel)
            else:
                unknown.append(rel)
            continue
        if actual is None:
            unknown.append(rel)
        elif actual == info["original_sha256"]:
            pristine.append(rel)
        elif actual == info["patched_sha256"]:
            patched.append(rel)
        else:
            unknown.append(rel)
    return pristine, patched, unknown


def make_backup(hermes_dir: str, manifest: dict[str, Any], *, config_path: str, log=print) -> str:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.expanduser(f"~/.hermes/backups/pre-hermes-feishu-card-{stamp}")
    os.makedirs(backup, exist_ok=True)
    for rel in manifest["files"]:
        src = os.path.join(hermes_dir, rel)
        if os.path.isfile(src):
            dst = os.path.join(backup, "files", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    # keep the current local diff (older port / uncommitted edits) and HEAD identity
    diff = run(["git", "-C", hermes_dir, "diff", "--", *manifest["files"]])
    if diff.stdout:
        with open(os.path.join(backup, "local.diff"), "w", encoding="utf-8") as fh:
            fh.write(diff.stdout)
    with open(os.path.join(backup, "HEAD"), "w", encoding="utf-8") as fh:
        fh.write(git_head(hermes_dir) + "\n")
    if os.path.isfile(config_path):
        shutil.copy2(config_path, os.path.join(backup, "config.yaml"))
    with open(os.path.join(backup, "META.txt"), "w", encoding="utf-8") as fh:
        fh.write("backup of Hermes files before Feishu streaming-card patch\n")
        fh.write(f"created: {stamp}\n")
        fh.write(f"hermes_dir: {hermes_dir}\n")
        fh.write(f"head: {git_head(hermes_dir)}\n")
    log(f"  backup: {backup}")
    return backup


def restore_head_targets(hermes_dir: str, manifest: dict[str, Any], log=print) -> None:
    paths = [rel for rel in manifest["files"] if not manifest["files"][rel].get("new_file")]
    # Restore BOTH the index and the worktree: an earlier hand port may have been
    # staged, and patch(1) edits the worktree only, leaving a staged-vs-worktree
    # split that a later `hermes update` would trip over.
    proc = run(["git", "-C", hermes_dir, "restore", "--source=HEAD", "--staged", "--worktree", "--", *paths])
    if proc.returncode != 0:
        proc = run(["git", "-C", hermes_dir, "restore", "--source=HEAD", "--worktree", "--", *paths])
    if proc.returncode != 0:
        raise SystemExit(f"git restore failed: {proc.stderr}")
    for rel in manifest["files"]:
        if manifest["files"][rel].get("new_file"):
            path = os.path.join(hermes_dir, rel)
            if os.path.isfile(path):
                os.remove(path)
    log("  restored pinned pristine targets from HEAD (backed up above)")


def apply_patch(hermes_dir: str, version: str, log=print) -> None:
    patch_path = os.path.join(REPO_DIR, "patches", version, "hermes-feishu-card.patch")
    patch_bin = shutil.which("patch")
    if patch_bin:
        with open(patch_path, "rb") as fh:
            proc = subprocess.run([patch_bin, "-p1", "--batch", "--forward", "--directory", hermes_dir],
                                  stdin=fh, capture_output=True, text=True)
        if proc.returncode == 0:
            log(f"  applied patch (patch -p1): {os.path.basename(patch_path)}")
            return
        log(f"  patch(1) failed, trying git apply:\n{proc.stdout}\n{proc.stderr}")
    git = shutil.which("git")
    if git:
        # git apply reads the patch by path; --check first so a failure leaves the tree clean.
        proc = run([git, "-C", hermes_dir, "apply", "--check", patch_path])
        if proc.returncode == 0:
            run([git, "-C", hermes_dir, "apply", patch_path], check=True)
            log(f"  applied patch (git apply): {os.path.basename(patch_path)}")
            return
        raise SystemExit(f"git apply --check failed: {proc.stderr}")
    raise SystemExit("neither patch(1) nor git is available to apply the patch")


def verify_patched(hermes_dir: str, manifest: dict[str, Any], log=print) -> None:
    bad = []
    for rel, info in manifest["files"].items():
        actual = sha256_file(os.path.join(hermes_dir, rel))
        if actual != info["patched_sha256"]:
            bad.append(rel)
    if bad:
        raise SystemExit("post-patch hash verification failed:\n" + "\n".join(f"  - {rel}" for rel in bad))
    log(f"  verified patched hashes: {len(manifest['files'])} files")


def compile_targets(hermes_dir: str, manifest: dict[str, Any], log=print) -> None:
    py = resolve_python(hermes_dir)
    files = [os.path.join(hermes_dir, rel) for rel in manifest["files"]]
    proc = run([*py, "-m", "py_compile", *files])
    if proc.returncode != 0:
        raise SystemExit(f"py_compile failed:\n{proc.stdout}\n{proc.stderr}")
    log(f"  byte-compiled {len(files)} files with {py[0]}")


def restart_gateway(log=print) -> bool:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        log("  restart: systemctl not found — restart the gateway manually")
        return False
    active = run([systemctl, "--user", "is-active", "hermes-gateway"]).stdout.strip()
    if active not in ("active", "activating", "reloading"):
        log(f"  restart: hermes-gateway is '{active or 'unknown'}' — start it manually "
            f"('systemctl --user start hermes-gateway')")
        return False
    proc = run([systemctl, "--user", "restart", "hermes-gateway"])
    if proc.returncode != 0:
        log(f"  restart: FAILED ({proc.stderr.strip()}) — run "
            f"'systemctl --user restart hermes-gateway' manually")
        return False
    import time
    for _ in range(20):
        time.sleep(0.5)
        state = run([systemctl, "--user", "is-active", "hermes-gateway"]).stdout.strip()
        if state == "active":
            log("  restart: hermes-gateway is active again")
            return True
    log("  restart: service did not report active within 10s — check 'journalctl --user -u hermes-gateway'")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="install.sh",
        description="Install the exact-build Hermes Feishu streaming-card patch.",
    )
    ap.add_argument("--hermes-dir", default=os.environ.get("HERMES_DIR", DEFAULT_HERMES_DIR),
                    help=f"Hermes checkout to patch (default: {DEFAULT_HERMES_DIR})")
    ap.add_argument("--version", default=DEFAULT_VERSION, help="patch build to install")
    ap.add_argument("--config", default="",
                    help="gateway config.yaml (default: <hermes home>/config.yaml)")
    ap.add_argument("--force", action="store_true",
                    help="back up local modifications and restore HEAD targets before patching")
    ap.add_argument("--skip-config", action="store_true",
                    help="do not touch config.yaml (card transport stays inactive)")
    ap.add_argument("--no-footer", action="store_true",
                    help="do not enable the per-platform runtime footer note")
    ap.add_argument("--no-restart", action="store_true",
                    help="do not restart the hermes-gateway user service")
    ap.add_argument("--dry-run", action="store_true",
                    help="run every check, print the plan, change nothing")
    args = ap.parse_args()

    hermes_dir = os.path.abspath(os.path.expanduser(args.hermes_dir))
    hermes_home = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
    config_path = os.path.abspath(os.path.expanduser(args.config)) if args.config else \
        os.path.join(hermes_home, "config.yaml")
    manifest = load_manifest(args.version)

    print(f"hermes-feishu-card patch {args.version}")
    print(f"  checkout: {hermes_dir}")
    print(f"  config:   {config_path}")

    if not os.path.isdir(hermes_dir):
        raise SystemExit(f"not a directory: {hermes_dir}")
    if not os.path.isfile(os.path.join(hermes_dir, "hermes_cli", "main.py")):
        raise SystemExit(f"not a Hermes checkout: {hermes_dir}")

    head = git_head(hermes_dir)
    if not head:
        raise SystemExit("not a git checkout (git rev-parse HEAD failed)")
    if head != manifest["upstream_commit"]:
        print(
            "Refusing to patch: this checkout is not the pinned upstream build.\n"
            f"  expected HEAD: {manifest['upstream_commit']}\n"
            f"  actual HEAD:   {head}\n"
            "Re-derive the patch for this build (see README.md -> 'Upgrading to a new Hermes build')."
        )
        return 2
    print(f"  upstream: {head} (pinned)")

    dirty = git_dirty_targets(hermes_dir, list(manifest["files"]))
    pristine, patched, unknown = inspect_state(hermes_dir, manifest)
    if patched and not pristine and not unknown:
        print("Already patched — nothing to do.")
        return 0
    if patched and (pristine or unknown):
        if not args.force:
            print(
                "Refusing to patch: checkout is PARTIALLY patched "
                f"({len(patched)} patched / {len(pristine)} pristine / {len(unknown)} unknown).\n"
                "Restore the checkout to the pinned build first (git restore --source=HEAD -- <files>), "
                "or re-run with --force to rebuild from HEAD."
            )
            return 2
        print("  checkout carries an older/partial patch build; rebuilding from HEAD (backed up):")
        for rel in unknown:
            print(f"  - {rel}")
    if unknown:
        if not args.force:
            print("WARNING: target files differ from the pinned pristine build (older hand port?):")
            for rel in (unknown + [rel for rel in dirty if rel not in unknown]):
                print(f"  - {rel}")
            print("Re-run with --force to back them up and restore HEAD before patching.")
            return 2
        print("  local modifications will be backed up and restored to HEAD:")
        for rel in (unknown + [rel for rel in dirty if rel not in unknown]):
            print(f"  - {rel}")

    if args.dry_run:
        print("dry-run: checks passed; would back up, patch, activate config, restart")
        return 0

    backup = make_backup(hermes_dir, manifest, config_path=config_path)
    try:
        restore_head_targets(hermes_dir, manifest)
        apply_patch(hermes_dir, args.version)
        verify_patched(hermes_dir, manifest)
        compile_targets(hermes_dir, manifest)
    except BaseException as exc:  # roll back a half-applied tree
        print(f"install failed ({exc}); restoring the pre-install files from the backup")
        for rel in manifest["files"]:
            src = os.path.join(backup, "files", rel)
            dst = os.path.join(hermes_dir, rel)
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
            elif os.path.isfile(dst):
                os.remove(dst)
        raise
    if not args.skip_config:
        activate_config(config_path, enable_footer=not args.no_footer)
    if not args.no_restart:
        restart_gateway()
    patch_sha = manifest.get("patch_sha256", "")
    print("\nInstalled OK.")
    print(f"  patch sha256:   {patch_sha}")
    print(f"  backup:         {backup}")
    print("  rollback:       ./uninstall.sh --backup " + backup)
    print("  verify:         ./tools/verify_patch.py --check-dir " + hermes_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
