# Agent instructions — hermes-feishu-card-patch

This repository installs a build-pinned patch that turns Hermes Agent's Feishu
replies into ONE streaming interactive card (tool progress + narration folded
into a status block, final answer + runtime-footer note in the same card), with
the presentation of `Leorand-dev/openclaw-feishu-patch`.

Use the steps below when a user asks to install / verify / roll back the patch.

## Preconditions

- Target checkout: `~/.hermes/hermes-agent` at upstream commit
  `ca069e12239a2c8d13887ac9f983557d00bdcfd2` (verified via `git rev-parse HEAD`).
  Fleet hosts with a different upstream build use the matching vendored variant:
  Hermes **v0.22.5** `bf0203a794a07aac3ff764972ab9087baa4e4f4e` → add `--version bf0203a7`;
  the older build `ccc695635094d7615373db267b77f894d978b1a0` maps to
  `--version ccc6956`. Exact-build manifests still check HEAD + per-file hashes.
- Target files must be the pinned pristine build (installer checks per-file SHA-256).
  If an older hand-written card port / other patch modified them, use `--force`
  (backs up files + `git diff`, restores HEAD, then patches).
- `lark-oapi >= 1.6.8` (ships `im.v1.message.patch`) must be in the checkout venv.
- Feishu credentials live in `~/.hermes/.env`; never copy them into this repo.

## Install

```sh
cd <this-repo>
./install.sh --dry-run     # checks only
./install.sh               # pinned build ca069e1223
./install.sh --version bf0203a7   # v0.22.5 target build
./install.sh --version ccc6956    # older ccc69563 build
```

Expected success lines:

```
applied patch (patch -p1): hermes-feishu-card.patch
verified patched hashes: 10 files
byte-compiled 10 files with ...
config: activated card transport + Feishu runtime footer in .../config.yaml
restart: hermes-gateway is active again
Installed OK.
```

Config activation sets `platforms.feishu.card_replies: true` and
`display.platforms.feishu.runtime_footer.enabled: true` (fields
`[model, context_pct, cache, tok_s, latency]`). `--skip-config` / `--no-footer` disable that.

## Verify

```sh
./tools/verify_patch.py --check-dir ~/.hermes/hermes-agent
python3 tests/test_card_stream.py
cd ~/.hermes/hermes-agent && PYTHONPATH=. .venv/bin/python <repo>/tests/test_card_integration.py
cd ~/.hermes/hermes-agent && PYTHONPATH=. .venv/bin/python <repo>/tools/live_card_probe.py
```

If `.venv/bin/python` re-execs into the tools interpreter on this host (diagnostic scripts
then fail with `No module named 'ruamel'`), use the resolved interpreter with the venv
site-packages: `PYTHONPATH="$PWD:$PWD/.venv/lib/python3.14/site-packages"
~/.hermes/tools/python-3.14.7+20260901-linux-x64/bin/python3 <repo>/tools/live_card_probe.py`.

Expected: verify `RESULT: OK`; 29/29 unit tests; 24/24 integration tests; live probe
prints `mid details/status: OK` and `final summary/details: OK` (status top with a
transient-provider notice, streaming `🧾 运行详情` note, final `hr` + `> ✅ 完成 …` summary
+ `主要工具` div for ≥3 tools, pipe-table `column_set`, footer duration once) and recalls
its probe card. Clarify prompts are interactive cards (choices + Other + Skip; multi-select
toggle + Submit); slash confirms render Approve Once / Always / Cancel buttons. CardKit 2.0
is opt-in (`FEISHU_CARDKIT_STREAMING=1`) and requires Feishu client ≥ 7.20.
`tools/card_health.py` summarizes card metrics; `tools/derive_build.py` derives a patch
for a new upstream commit.

## Rollback

```sh
./uninstall.sh                # restore the state before the last install (newest backup)
./uninstall.sh --to-pristine  # remove the patch entirely: targets restored from git HEAD
```

Restores files + config.yaml, removes `card_stream.py`, clears stale `.pyc`,
verifies hashes, restarts the gateway. Use `--to-pristine` after several re-installs
(the newest backup contains the previous patched build).

## Guardrails

- Do **not** apply to a different upstream commit; re-derive the patch first
  (see README "升级到新的 Hermes 构建").
- Do **not** add config keys inside `platforms.feishu.extra`; the adapter reads
  `card_replies` from `platforms.feishu` (top level of the platform block) or
  `FEISHU_CARD_REPLIES=1`.
- Do **not** put credentials, tokens or chat contents into this repository.
- A running gateway only picks up the patch after a restart (`systemctl --user
  restart hermes-gateway`); in-flight turns are interrupted by that restart.
