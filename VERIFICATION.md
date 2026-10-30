# VERIFICATION — hermes-feishu-card-patch on Hermes `ca069e1223`

Evidence for the pinned build: Feishu API behaviour the transport relies on, patch
integrity, test suites, installer/rollback checks, and the live deployment on the
target host.

- Reference presentation: [`Leorand-dev/openclaw-feishu-patch`](https://github.com/Leorand-dev/openclaw-feishu-patch)
  (2026.10.5, 29 hunks, 415 assertions) → same user-visible behaviour, Hermes implementation.
- Target upstream: `NousResearch/hermes-agent` — pinned build `ca069e12239a2c8d13887ac9f983557d00bdcfd2`;
  the live host-a host now runs **v0.22.5** `bf0203a794a07aac3ff764972ab9087baa4e4f4e`
  (vendored build `patches/bf0203a7`, see §5.4).
- Patch: `patches/ca069e1223/hermes-feishu-card.patch` — 50 hunks, 8 modified files +
  2 new files, sha256 `330eef8902549636505943b3da18913b034d05fff0b51e7254f2387677e33a17`.
  v0.22.5 variant: 59 hunks, sha256
  `b1b5af857387082acfac3d5abef05b8ac1e37a59991613be951410b27ce3875b`.

## 1. Feishu API facts (verified live on the target host with the real app creds)

| fact | evidence |
| --- | --- |
| `lark-oapi==1.6.8` in the checkout venv already ships `PatchMessageRequest`/`PatchMessageRequestBody` and `client.im.v1.message.patch` | import check in `.venv` |
| card send: `msg_type="interactive"`, `content=json.dumps(card)` (a JSON **string**) | live send to the home chat → code 0 with message id; reply() anchors `parent_id`/`root_id` |
| in-place update: **PATCH** `/open-apis/im/v1/messages/:message_id`, body `{"content":"<card JSON string>"}` | live patch of body/status/header → code 0, `update_time` changes (~0.4–0.5 s) |
| `PUT message.update` rejects interactive messages | live → `230001 invalid msg_type` |
| content must be a JSON string, not a dict | live → `9499 Invalid parameter type in json: content` |
| `config.update_multi: true` required on send and every patch (shared-card visibility) | docs + live probe; enforced on every frame by this patch |
| classic card JSON 1.0 (`config/header/elements`, `markdown`/`hr`/`note`) accepted by send **and** patch | live probe with this patch's exact JSON |
| limits: card content ≤30 KB, patch only ≤14 days, ~5 QPS patch; markdown elements ~4000 chars | docs + live 8-patch / 39.7 KB probe; this patch caps card JSON at 28 KB and splits the body at 4000 chars |
| PATCH preserves the reply/thread anchor; `message.delete` recalls the card | live: `parent_id`/`root_id` unchanged; probe cards recalled cleanly |

## 2. BEFORE state on the target chat (2026-10-19 23:59:10 +0800)

Latest 50 messages in the Feishu home chat: **48 interactive bot cards + 2 user text**.
The last long agent turn produced **30 separate card messages** (8 progress / 22 final-ish)
over 12.5 minutes, with 24 tool-progress lines inside card markdown; 21 of the cards shared
one reply root. Replies were already cards, but fragmented — the exact
“wall of bubbles / one answer, many cards” failure mode the reference patch removes.
(An independent 50-message audit right before deployment still showed up to 8 cards in
one turn.)

## 3. Patch integrity (pristine extraction of upstream HEAD)

```
patch -p1 --dry-run  → 10 files check OK
patch -p1            → 10 files patched
sha256 check          → patched-hash mismatches: 0 of 10
patch -p1 -R         → 10 files reversed
sha256 check          → original-hash mismatches: 0
RESULT: PATCH OK
```

`tools/verify_patch.py` reports patch sha256 OK, 49/49 hunks, installed-tree hashes OK,
pristine-tree hashes OK (after rollback). This was re-run after the second-pass audit
(see §4.4 / `AUDIT.md`) and again for the v0.22.5 build (`--version bf0203a7`, 58/58
hunks, forward+reverse exact hashes).

## 4. Tests (all run on the target host)

### 4.1 Card presentation state machine — `tests/test_card_stream.py`

```
29/29 tests passed
```

Covers: prewarm card shape (header / `update_multi` / status / prewarm string), first-tool
status, tool count + last-tool label + elapsed, always-on elapsed for prewarm/processing,
tool-detail + iteration (`第 N 轮`) rendering, narration ticker (latest only, ≤80 chars,
no surrogate split, no duplicate when it is the primary line), waiting→tool-done switch,
processing status, final card = `markdown + hr + note` with status removed, tool-only
final body, silent-turn discard predicate, >4000-char body element splitting,
bounded-body fitting/truncation notice, cumulative-text merge, change detection,
28 KB JSON size guard, pipe-table → `column_set`/wide-table list conversion (code fences
untouched), transient notice in status (cleared by tool progress), error-body red header.

### 4.2 Adapter + stream-consumer integration — `tests/test_card_integration.py`

Runs the **real** `FeishuAdapter` and `GatewayStreamConsumer` against an in-memory fake
lark client (no network, no credentials):

```
24/24 integration tests passed
```

1. non-streamed final response → one finished card + footer note;
2. non-streamed final over the card budget → chunked text fallback, no truncated card;
3. `send_footer` folds the footer into the live card (no second message);
4. `platforms.feishu.card_replies` YAML boolean parsing (bool/string forms);
5. one full tool+answer turn → **exactly one interactive send**, ≥2 in-place patches,
   tool progress visible in the mid-turn status, final frame without status element,
   answer exactly once, no delete;
6. commentary produces **one** status patch (no duplicate);
7. tool boundary / segment break → still ONE card, blocks separated by a paragraph break;
8. long answer → bounded card head + truncation notice + continuation **card(s)**; full
   body recorded as delivered; still one interactive head card plus tail cards;
9. card PATCH failure → downgrades without losing text (fallback delivers unseen tail);
10. prewarm rejection → falls back to text streaming, no interactive card landed;
11. tool-only turn → final body `✅ 已执行 2 个工具`;
12. silent/empty turn → the prewarmed card is deleted, nothing finalized;
13. 15 s elapsed heartbeat repaints the status without body changes;
14. burst of six tool events inside 0.25 s → zero intermediate PATCHes (rate floor
    coalesces; deferred state flushes on the next tick);
15. `card_replies: false` → falls back to the stock edit/text transport;
16. Feishu `230020` rate limit on two PATCH attempts → adapter backs off and retries,
    consumer keeps the card transport (`_use_card_streaming` stays True) and sends no
    fallback text;
17. step callback + tool detail → the rendered status contains `第 7 轮` and
    `terminal: uname -a …`;
18. clarify prompt → an interactive card with one button per choice + Other + Skip (not a
    plain text/post bubble);
19. clicking a clarify button resolves the pending `clarify_gateway` entry with the
    selected answer and retires the button card inline;
20. a finalize-only `edit_message` without card metadata on the live card folds the full
    answer into that card (Feishu `230054` previously forced a duplicate second card);
21. a final card with a pipe table renders `column_set` columns (no raw `|` rows);
22. a normal-lane final send (e.g. provider-error fallback) folds into the still-open live
    card and uses the red error header instead of creating a second card/bubble.

### 4.3 Installer / rollback (local clone at the pinned commit)

```
install --dry-run           → checks passed
install                     → backup → patch → 10 patched hashes OK → py_compile OK
                              → config: platforms.feishu.card_replies=true,
                                display.platforms.feishu.runtime_footer.enabled=true
install (again)             → "Already patched — nothing to do."
uninstall --backup <dir>    → original hashes OK, config restored, stale .pyc cleared
uninstall --to-pristine     → 10 files restored from git HEAD, new modules removed,
                              original hashes OK
verify --check-pristine     → RESULT: OK
--force path (simulated old hand port, 2 dirty targets)
  install (no --force)      → exit 2, lists dirty targets, changes nothing
  install --force           → backs up files + local.diff, restores HEAD (staged+worktree),
                              patches, verifies
  uninstall --backup        → restores the pre-install (hand-port) state
```

### 4.4 Second-pass audit (`AUDIT.md`)

A full audit of every hunk against the pinned upstream source found and fixed seven
behavioural issues, all with new regression tests (included in the 15 above):
segment-break separator missing in card mode; duplicate PATCH per commentary; unsafe
card→legacy downgrade (duplicate/lost tail); a success-without-message-id could open a
second card; long answers had no bounded-card + continuation path; a burst of tool
events could exceed Feishu's patch-rate cap; a failed card patch wasted a doomed PUT on
the known card message. Plus: `MEDIA:` directives are now stripped from card bodies,
`tool_progress: off` is respected, and dead card helpers were removed. Approval-card
behaviour is untouched by design. Installer also gained staged+worktree restore and
`uninstall --to-pristine`.

## 5. Live deployment on host-a (2026-10-20)

The host already carried an older hand-written card port; the installer replaced it
safely (`--force`: backup + `git restore --source=HEAD --staged --worktree` + patch).

### 5.1 First rollout — patch build `413c3bc6…`

```
backup: $HOME/.hermes/backups/pre-hermes-feishu-card-<timestamp>
applied patch (patch -p1): hermes-feishu-card.patch
verified patched hashes: 10 files
byte-compiled 10 files with $HOME/.hermes/hermes-agent/.venv/bin/python
config: platforms.feishu.card_replies=true + display.platforms.feishu.runtime_footer.enabled=true
Installed OK.
```

Gateway restarted in an idle window (no turn interrupted), MainPID <redacted>,
`ActiveEnterTimestamp=Tue 2026-10-20 00:25:35 CST`:

```
[Feishu] Streaming-card replies enabled: one interactive card per reply
         (platforms.feishu.card_replies=true)
[Feishu] Connected in websocket mode (feishu)
✓ feishu connected
Gateway running with 1 platform(s)
```

Live card probe through the patched adapter with the real bot credentials
(send → patch mid → patch final → footer note → recall; no residue left):

```
send prewarm card: success=True
patch mid card:    success=True err=None
patch final card:  success=True err=None
patch footer note: success=True err=None
recall probe card: True
LIVE CARD PROBE: OK
```

**Real user turn (00:45:45–00:47:11) on the live build** — the acceptance check:

```
user text:  补丁车上 / 补丁测试
gateway:    Suppressing normal final send ... streamed=True content_delivered=True
chat audit: turn root om_x100b6250dd4478a4b0f2 → cards=1, patched_in_place=1
            turn root om_x100b6250da3b70a0cf8b → cards=1, patched_in_place=1
```

One card per reply, patched in place — the exact presentation this patch targets.

### 5.2 Second rollout — audited build `e580aebd…` (this repository state)

```
backup: $HOME/.hermes/backups/pre-hermes-feishu-card-<timestamp>
applied patch (patch -p1): hermes-feishu-card.patch
verified patched hashes: 10 files
Installed OK.  patch sha256: e580aebda7f799e48852155f121a31decbacfadf3fbe3543335365c81be872c1
```

Restarted in an idle window at `2026-10-20 00:48:21 CST`, MainPID <redacted>; startup log
again shows `[Feishu] Streaming-card replies enabled …` and a clean websocket connect,
no errors after restart. The audited build's live probe and installed hashes:

```
tools/verify_patch.py --check-dir ~/.hermes/hermes-agent
  patch sha256: OK; hunks: OK (41/41)
  all 10 patched hashes match
  RESULT: OK

send prewarm card: success=True
patch mid card:    success=True err=None
patch final card:  success=True err=None
patch footer note: success=True err=None
recall probe card: True
LIVE CARD PROBE: OK
```

Post-restart chat audit: `one-card turns: 19`, `max cards in one turn: 7` (that maximum
belongs to pre-patch historical roots; every post-patch root audited so far is
`cards=1, patched_in_place=1`). Re-run `tools/chat_audit.py` after the next user turn as
the continuing acceptance check.

### 5.3 Fleet rollout — prewarm text `⏳ 思考中…` (2026-10-20 16:59–17:01)

Prewarm wording changed from `⏳ 已收到，正在处理…` to `⏳ 思考中…` and pushed to the
three fleet gateways. Build selection is automatic per host's upstream commit:

| host | upstream HEAD | build installed | patch sha256 |
| --- | --- | --- | --- |
| host-a | `ca069e1223` (pinned) | `ca069e1223` | `bdb2a831…` |
| host-b | `ca069e1223` (pinned) | `ca069e1223` | `bdb2a831…` |
| host-c | `ccc69563` (pin +1523 commits) | vendored `ccc6956` | `5688c0d3…` |

- **host-a** — install 16:59:07 (9 hashes OK, backup `pre-hermes-feishu-card-<timestamp>`),
  systemd restart 16:59:31 (PID <redacted>); startup log `Streaming-card replies enabled …`,
  Feishu websocket connected, no errors; `verify --check-dir` 9/9; 17/17 card unit tests.
- **host-b** — install 16:59:20 (9 hashes OK, backup `<timestamp>`); gateway runs as a manual
  orphan process here (no systemd user unit), restarted old PID <redacted> → new PID <redacted>;
  startup log `Streaming-card replies enabled …`, Feishu connected, no errors; hashes 9/9;
  17/17 card unit tests.
- **host-c** — different upstream (`ccc69563`); the vendored exact-build variant was applied at
  17:01:08 (9 hashes OK, backup `<timestamp>`); manual restart old PID <redacted> → new PID <redacted> at 17:01:14; startup log `Streaming-card replies enabled …`, Feishu + WeCom
  connected, no errors; variant verify 9/9; 17/17 card unit tests.
- Variant patch integrity on host-c: dry-run → apply → reverse on a pristine `HEAD` extraction
  produced exact patched hashes (9/9) and exact original hashes (9/9).
- All three gateways were idle before restart; no in-flight turns were interrupted.
- Prewarm string verified in every installed `card_stream.py`:
  `STATUS_PREWARM = "⏳ 思考中…"`.

### 5.4 v0.22.5 P0–P3 rollout — host-a (2026-10-25 00:16–00:19)

The host was upgraded to Hermes **v0.22.5** (`bf0203a794a07aac3ff764972ab9087baa4e4f4e`)
and its adapted port was re-patched with the audit follow-up (P0 rate-limit resilience,
P1 richer card status, P2 bubble suppression, P3 footer latency):

- Patch build: `patches/bf0203a7`, 46 hunks, sha256
  `56c866ab8e680c7e05265a8fee08c494a6b2dbb91b18008a4c8c48126d8b9632`;
  10/10 patched hashes match after install.
- Install: 00:19:09 (force rebuild from the v0.22.5 `HEAD`; backup
  `pre-hermes-feishu-card-<timestamp>`).
- Gateway restarted **00:19:11 CST** (PID <redacted>, active); startup log
  `[Feishu] Streaming-card replies enabled …`; Feishu websocket connected; no errors
  after restart.
- Footer config (platform override): `[model, context_pct, cache, tok_s, latency]`.
- Live card probe through the patched adapter (real credentials):
  `send prewarm card success=True` → `patch mid card success=True` →
  **`mid status fields: iteration/detail=OK`** (the card really renders `第 7 轮` and
  `terminal: uname -a …`) → `patch final card success=True` → `patch footer note success=True`
  → `recall probe card: True` → `LIVE CARD PROBE: OK`.
- Behaviour shipped: 230020 backoff (1.2s/2.4s/4.8s) instead of downgrading, 1.0 s patch
  floor, always-on elapsed, `第 N 轮`, tool-argument detail, gateway heartbeat/`Writing`
  suppression while the card is active, final-frame retry + stale-card discard,
  footer latency; 19/19 unit + 17/17 integration tests.
- Variant patch integrity: dry-run → apply → reverse on a pristine `bf0203a7` extraction
  produced exact patched hashes (9/9) and exact original hashes (9/9).

### 5.5 Fleet rollout of the v0.22.5 build — host-b / host-c (2026-10-25 01:08–01:10)

Both hosts were already at the same v0.22.5 `HEAD` with the older adapted port; the
P0–P3 build was installed there too and their gateways restarted through the system-level
`hermes-gateway.service` (single supervised process, no manual orphan left behind):

| host | install | restart | result |
| --- | --- | --- | --- |
| host-b | 01:08:25, backup `pre-hermes-feishu-card-<timestamp>` | systemd 01:09:15, PID <redacted> | 9/9 hashes OK; `Streaming-card replies enabled …`; Feishu connected; footer `[…, latency]` |
| host-c | 01:09:44, backup `pre-hermes-feishu-card-<timestamp>` | systemd 01:09:51, PID <redacted> | 9/9 hashes OK; card line logged; Feishu + WeCom connected; footer `[…, latency]` |

- `host-b` needed one extra reconciliation: the restart helper first saw no **user** service and
  started a manual process while the existing system-level service also came back. The
  manual PID was terminated; systemd's instance (PID <redacted>) is the only gateway running
  now.
- `host-c` had no `.venv`; install/verify used `/usr/bin/python3` with the checkout's
  `venv/lib/python3.11/site-packages` on `PYTHONPATH` only for the ruamel-YAML config step.
- The only log entries after restart are the normal shutdown `Lark: receive message loop
  exit` lines from the SIGTERM restart and the pre-existing SSH password-auth security
  audit warning — no gateway/card errors.

### 5.6 Presentation refresh to reference 2026.10.6 — fleet (2026-10-25 15:50–15:52)

The reference project updated its layout (status top, `🧾 运行详情` panel, final
completion summary, chat preview). That layout was ported to the v0.22.5 build
(`patches/bf0203a7`, 55 hunks, sha256 `5c3bf1a1…`) and installed on all three hosts:

| host | install | restart | result |
| --- | --- | --- | --- |
| host-a | 15:50:30, backup `<timestamp>` | user systemd 15:50:32, PID <redacted> | 9/9 hashes OK; card line logged; Feishu connected |
| host-b | 15:50, backup `…-1550xx` | system systemd 15:51:12, PID <redacted> | 9/9 hashes OK; card line logged; Feishu connected |
| host-c | 15:50, backup `…-1550xx` | system systemd 15:51:48/55, PID <redacted> | 9/9 hashes OK; card line logged; Feishu + WeCom connected |

- Live card probe on the installed host-a build:
  `mid details/status: OK` (status top, `🧾 运行详情 · 共 1 次：💻terminal×1 · 💭 …`),
  `final summary/details: OK` (`> ✅ 完成 …`, details line, reasoning hidden, recall OK) →
  `LIVE CARD PROBE: OK`.
- Classic Feishu interactive cards accept `collapsible_panel` at the API level, but the
  client renders an "upgrade client" placeholder for it. The same information is therefore
  rendered as a compact grey `🧾 运行详情` note; a truly collapsed panel needs the CardKit
  2.0 element API, which this transport does not use.
- Tests for this build: 20/20 unit + 20/20 integration; both patch builds forward/reverse
  byte-exact.
- All three fleet hosts now run patch sha256
  `1920c6d868f9bc9508f9d21972bc16b32fea5eaf4ffd41e4a25ed70728cedf50` (clarify + final-fold
  follow-up, §5.7).

### 5.8 Final-card layout redesign — fleet (2026-10-25 17:43–17:44)

The 17:39 final card showed the remaining layout problem: the grey `🧾 运行详情` line was a
single clipped line mixing tool counts, errors **and** a markdown reasoning table
(`💭 | **82** | ...`), so everything was crammed together. Redesigned final layout:

```
answer body
hr
> ✅ 完成 · N 个工具 · [M 个错误] · elapsed      (separate blockquote)
div  🧾 运行详情
     fields: **🔧 工具** 共 N 次 | **⏱ 耗时** 1m02s
             **主要工具** 💻 terminal×14 · 📖 read file×4 · …
             **⚠️ 错误** … (only when errors)
hr + grey footer note (model · context% · cache · tok/s · latency)
```

- reasoning is no longer rendered into the card (it was the main source of the cramped
  line); streaming frames keep only a short grey tools/errors note;
- the final `stats` block travels in every payload, so the stale-finalize/final-fold path
  also renders the same roomy div fields and completion summary (no second card);
- tool-only turns use the summary block as the first element (no empty placeholder).

| host | install | restart | result |
| --- | --- | --- | --- |
| host-a | 17:43:00, backup `<timestamp>` | user systemd 17:43:02, PID <redacted> | 9/9 hashes OK; card line logged; Feishu connected |
| host-b | 17:43:47, backup `<timestamp>` | system systemd 17:43:47, PID <redacted> | 9/9 hashes OK; card line logged; Feishu connected |
| host-c | 17:44:21, backup `<timestamp>` | system systemd 17:44:22, PID <redacted> | 9/9 hashes OK; card line logged; Feishu + WeCom connected |

Live probes on installed host-a: `mid details/status: OK`, `final summary/details: OK`
(`> ✅ 完成 …` + structured details, no reasoning, recall OK) → `LIVE CARD PROBE: OK`;
clarify card probe OK. Tests: 20/20 unit + 20/20 integration.

### 5.7 Clarify-as-card + final-fold follow-up — fleet (2026-10-25 17:26–17:29)

A real host-a turn (16:18–16:33) exposed two presentation bugs in the logs:

1. the clarify prompt ("❓ 刚查过… + options + Reply skip") arrived as a **post bubble
   outside the card** because the Feishu adapter used the base text `send_clarify`
   fallback;
2. the gateway's stale-finalize reconciliation tried a plain-text edit of the interactive
   card (`[230054] This operation is not supported for this message type`), then sent the
   complete answer via the normal final path, which produced a **second card** for the
   same answer.

Fixes (patch builds above):

- `send_clarify` override: multiple-choice prompt is an interactive card (one button per
  choice + Other + Skip); open-ended/multi-select prompt is a card asking for a typed
  reply (the gateway text intercept still resolves it); clicks resolve
  `clarify_gateway.resolve_gateway_clarify` and update the card inline; a
  `retire_clarify_card` hook replaces stale buttons when the prompt ends without a click.
- `edit_message(finalize=True)` without card metadata now folds the answer into the known
  live card instead of PUT-ing text at it, so stale-finalize/transformed finals do not
  create a duplicate card.

| host | install | restart | result |
| --- | --- | --- | --- |
| host-a | 17:26:47, backup `<timestamp>` | user systemd 17:26:49, PID <redacted> | 9/9 hashes OK; card line logged; Feishu connected |
| host-b | 17:27:56, backup `<timestamp>` | system systemd 17:27:56, PID <redacted> | 9/9 hashes OK; card line logged; Feishu connected |
| host-c | 17:28:31, backup `<timestamp>` | system systemd 17:28:32, PID <redacted> | 9/9 hashes OK; card line logged; Feishu + WeCom connected |

Live probes on the installed host-a build: card layout
`mid details/status: OK` / `final summary/details: OK` / `LIVE CARD PROBE: OK`; clarify
prompt `send_clarify success=True` + `clarify card fields: OK` (question, staging/prod
buttons, Other, Skip) + card recalled. Tests: 20/20 unit + 20/20 integration.

### 5.9 Duration dedupe + small-turn details threshold — fleet (2026-10-25 18:05–18:06)

The 17:52/17:54 final card confirmed two remaining annoyances the other reviewer also
flagged: elapsed time appeared **three times** (summary `2m29s`, details `⏱ 2m29s`,
footer latency `2m30s`) and a 1–2 tool turn still rendered a whole details block.

Changes:

- final summary is now `> ✅ 完成 · N 个工具 · [M 个错误]` — no duration;
- details div has no `⏱ 耗时` and no duplicated `共 N 次` field; it shows only
  `主要工具` and `⚠️ 错误`, and only when `tool_total >= 3` or an error occurred;
- the footer latency remains the **single** duration shown on the card;
- reasoning capture/rendering and the dead `DETAILS_*_HEADER` constants were removed
  entirely (streaming and final frames no longer carry a `💭` line).

| host | install | restart | result |
| --- | --- | --- | --- |
| host-a | 18:04:59, backup `<timestamp>` | user systemd 18:05:01, PID <redacted> | 9/9 hashes OK; card line logged; Feishu connected |
| host-b | 18:05:43, backup `<timestamp>` | system systemd 18:05:44, PID <redacted> | 9/9 hashes OK; card line logged; Feishu connected |
| host-c | 18:06:16, backup `<timestamp>` | system systemd 18:06:18, PID <redacted> | 9/9 hashes OK; card line logged; Feishu + WeCom connected |

Live host-a probe: `mid details/status: OK` (3 tools), `final summary/details: OK`
(`> ✅ 完成 · 3 个工具`, details only `主要工具`, footer `3m12s` once), clarify card OK.
Tests: 20/20 unit + 20/20 integration.

### 5.10 Tables, continuation cards, transient notices — fleet (2026-10-30 21:33–21:35)

The 5-day audit (35 turns, 18 user messages) found the remaining presentation gaps:
pipe tables rendered as raw `|` text, >8000-char tails were normal messages, and provider
retry notices arrived as 4 separate text bubbles per outage.

Implemented:

- **pipe tables** → `column_set` for ≤4 columns (one column per header, values stacked),
  or “headers + bullet list” for wider tables; fenced code blocks are untouched;
- **continuation cards**: overflow is split into ≤6000-char chunks and sent as
  `<title> · 续 N/M` final cards (no loose text tail);
- **diagnostics in card**: `DiagnosticText` statuses (provider retry / redirect /
  refusal) go to the card status line via `on_card_notice`; routine progress still folds
  silently; typed/recovered progress clears the notice;
- **final fallback fold**: a normal-lane final send (failed finalize or failed turn)
  folds into the still-open live card; `⚠️/❌` bodies get the red header. No
  “half-written card + error bubble” duplicate.

| host | install | restart | result |
| --- | --- | --- | --- |
| host-a | 21:33:44, backup `<timestamp>` | user systemd 21:33:46, PID <redacted> | 9/9 hashes OK; card line logged; Feishu connected |
| host-b | 21:34:33, backup `<timestamp>` | system systemd 21:34:33, PID <redacted> | 9/9 hashes OK; card line logged; Feishu connected |
| host-c | 21:35:07, backup `<timestamp>` | system systemd 21:35:09, PID <redacted> | 9/9 hashes OK; card line logged; Feishu + WeCom connected |

Live host-a probe: 3-tool turn + notice status → `mid details/status: OK`; final card with a
pipe table → `final summary/details: OK` (`> ✅ 完成 · 3 个工具`, `主要工具` details,
column_set table, footer `3m12s` once); clarify card OK. Tests: 25/25 unit + 22/22
integration.

### 5.11 P1/P2: capsules, tool errors, clarify/slash buttons, CardKit 2.0 — fleet (2026-10-30 22:08–22:10)

P1 shipped:

- header status capsule in the title (`· 运行中` / `· 完成` / `· 出错`, red on errors);
- tool error reason in the final details (`执行失败：<detail 首行>`, ≤90 chars);
- multi-select clarify card: per-choice ✅/○ toggle + `✅ 提交（N）`, resolves a JSON label
  array through `clarify_gateway` (typed “1,3” still works);
- destructive slash-confirm card (`/new`, `/clear`, …) with Approve Once / Always / Cancel,
  resolving via `tools.slash_confirm.resolve()`;
- final card retry 0/1.5/3/6s (incl. 230020); on total failure the card is kept for the
  normal-final fold/recall path instead of being deleted first.

P2 shipped:

- optional CardKit 2.0 transport (`platforms.feishu.cardkit_streaming` /
  `FEISHU_CARDKIT_STREAMING=1`): card entity create/send, `card_element.content` streaming
  of status/body, settings close, final full-card swap with native `table` and
  `collapsible_panel`, status `text_tag_list`. Live API probe on host-a:
  create/send/content×2/settings/final update all `code=0`. **Default off** because this
  Feishu client renders a “请升级至最新版本客户端” block for 2.0 cards (client < 7.20);
  classic transport remains the default and the 2.0 path falls back on any failure.
- card observability: `feishu_card_metrics.jsonl` (`card_send_ok/failed`,
  `card_patch_ok/failed/rate_limited`, `cardkit_*`, `card_finalize_failed`) +
  `tools/card_health.py` summary.
- `tools/derive_build.py`: new-upstream derivation via git worktree + patch/fuzz, then
  `make_patch_variant.py`; tested on bf0203a7 and reproduced the same patch sha.

| host | install | restart | result |
| --- | --- | --- | --- |
| host-a | 22:08:46, backup `<timestamp>` | user systemd 22:08:48, PID <redacted> | 10/10 hashes OK; card line logged; Feishu connected |
| host-b | 22:09:32, backup `<timestamp>` | system systemd 22:09:33, PID <redacted> | 10/10 hashes OK; card line logged; Feishu connected |
| host-c | 22:10:08, backup `<timestamp>` | system systemd 22:10:09, PID <redacted> | 10/10 hashes OK; card line logged; Feishu + WeCom connected |

Live host-a classic probe: `mid details/status: OK`, `final summary/details: OK`,
clarify card OK. CardKit adapter probe: create/send/content/close/final/footer all success;
display intentionally remains classic until the client upgrades. Tests: 29/29 unit +
24/24 integration.

## 6. Known deviations from the OpenClaw reference

- Default classic card JSON 1.0 + whole-card PATCH (works on the current client).
  An optional CardKit 2.0 transport exists behind `cardkit_streaming`; it gives
  element-level status/body updates, native table and collapsible_panel, but this
  Feishu client (< 7.20) renders a “请升级至最新版本客户端” block, so it is **off by
  default** and falls back to classic on any error.
- Edit cadence keeps Hermes' 0.8 s + adaptive flood backoff (the reference's internal
  160 ms throttle is OpenClaw-specific).
- Reasoning is fully removed from the card (streaming and final): classic cards have no
  collapsible panel, and the one-line grey preview was the source of the cramped 17:39
  card.
- Markdown pipe tables are converted to `column_set` (≤4 columns) or header + bullet
  list (wider tables); code fences are untouched. >8000-char overflow is delivered as
  continuation cards (`· 续 N/M`), not loose text.
- Clarify prompts are interactive cards: single choice = one button per choice + Other
  + Skip; multi-select = ✅/○ toggle + Submit; open-ended prompts use a card asking for
  a typed reply (the gateway text intercept resolves it). Destructive slash commands use
  Approve Once / Always / Cancel buttons via `send_slash_confirm`.
- Tool progress depends on Hermes' `tool_progress` callback being enabled (Feishu tier
  default `new`); with `tool_progress: off` the card still works, showing only
  prewarm/generation status.
- Long answers whose card JSON exceeds the 28 KB budget fall back to the stock text
  transport for that frame instead of silently truncating.

## 7. Host-specific note for verifiers

On this host, invoking diagnostic scripts through the checkout's `.venv/bin/python`
**symlink** can re-exec into the Hermes tools interpreter (`sys.prefix` = tools python,
`PYTHONPATH` dropped), making `ruamel`/`lark_oapi` unimportable in that subprocess. The
gateway itself is unaffected. For the diagnostic scripts use the resolved interpreter plus
the venv site-packages explicitly:

```sh
H=~/.hermes/hermes-agent
PYTHONPATH="$H:$H/.venv/lib/python3.14/site-packages" \
  ~/.hermes/tools/python-3.14.7+20260901-linux-x64/bin/python3 \
  tools/live_card_probe.py
```
