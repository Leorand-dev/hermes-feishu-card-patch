# AUDIT — hermes-feishu-card-patch (patch build `ca069e1223`)

Second-pass review of the streaming-card patch requested after the live rollout.
Method: read every hunk against the pinned upstream source, trace the card transport
end-to-end (prewarm → deltas → tool/narration events → segment breaks → finalize →
footer), then exercise it with the real `FeishuAdapter` + `GatewayStreamConsumer`
against a fake lark client, including injected API failures. Approval-card handling was
**not** touched (explicitly out of scope; the adapter's approval code is upstream and
unchanged).

Result: **7 behavioural fixes / hardening changes, 3 cleanups, 2 installer improvements,
all covered by new regression tests.** 17 unit + 15 integration tests pass; the patch
still applies/reverses byte-exactly on the pinned build.

## 1. Findings and fixes

| # | Finding (severity) | Fix |
| --- | --- | --- |
| 1 | **Segment/tool boundaries concatenated answer blocks with no separator** — the card-mode loop branch never called `_end_segment`, so a preamble and the post-tool answer ran together (`前言正文`). | Card branch now calls `_end_segment` on a segment tick; it sets the pending separator and the next delta prepends `\n\n`. Regression: `test_segment_break_keeps_one_card` asserts the paragraph break. |
| 2 | **Duplicate PATCH per commentary** — the drain already rendered the narration, then `_deliver_commentary` forced a second identical patch. | Commentary path is non-forced; unchanged payload signature skips the second PATCH. Regression: `test_commentary_patches_card_once`. |
| 3 | **Card failure downgrade could duplicate or lose text** — on a rejected PATCH the consumer disabled card mode but left the legacy fallback without a prefix, so it could resend the whole answer (or drop the tail). | `_card_downgrade` now seeds the legacy fallback with the last visible body (`_enter_fallback_mode(prefix)`), so only the unseen tail is sent. Regression: `test_card_patch_failure_downgrades_without_losing_text`. |
| 4 | **A "successful" card send without a message id could open a second card** on the next frame. | Treated as a failure: downgrade, never send a second card. |
| 5 | **Long answers could exceed the card budget** — the card builder folded overflow into an oversized markdown element, and the >28 KB guard would drop the frame; no continuation path existed, so text could be lost/duplicated. | `card_stream.fit_card_body()` bounds the card body at 8000 chars with a pointer notice; the consumer sends the remainder as ordinary continuation message(s) at finalize and records the full body as delivered (gateway dedupe stays correct). Non-streamed finals over the budget fall back to the chunked text transport instead of a truncated card. Regressions: `test_long_answer_uses_card_head_and_continuation_messages`, `test_non_streamed_final_over_card_budget_falls_back_to_text`, `test_fit_card_body_truncates_and_points_to_continuation`. |
| 6 | **Patch-rate burst risk** — a rapid tool burst could exceed Feishu's documented ~5 QPS per-message PATCH cap. | Added a 0.25 s hard floor for all card frames (≤4/s) with a pending flag so deferred state changes are re-sent on the next tick; turn-final frames are exempt. Regression: `test_patch_rate_floor_coalesces_tool_burst`. |
| 7 | **Wasted doomed PUT after a failed card patch** — `edit_message` fell through to a text update on a known interactive card (always `230001`). | Fail fast when the id belongs to a tracked live card, so the consumer downgrades immediately. Covered by the downgrade test. |
| 8 | **`MEDIA:` / audio directives could leak into the card body** — the card path bypassed the stock transport's display cleanup. | The consumer cleans the body with the same `_clean_for_display` before rendering; the non-streamed card path strips directives too. |
| 9 | **`tool_progress: off` was ignored** — card status still received tool events (contradicted the documented behaviour). | Card tool events are queued only when `tool_progress` is enabled; clarify/`_thinking` events stay out of the status ticker. |

Cleanups: removed dead card helpers (`_card_payload`, `on_body`, `needs_render`,
render-time bookkeeping fields, an unused elapsed-refresh helper), removed a duplicated
`set_body` call per tick, and made `hermes_card_stream` a shared constant instead of
scattered string literals.

## 2. Installer / rollback audit

- **Staged + worktree restore**: `--force` now `git restore --source=HEAD --staged
  --worktree` on the target files, so a previously staged hand port cannot leave an
  index/worktree split that breaks a later `hermes update`.
- **`uninstall.sh --to-pristine`**: restores the eight targets from git `HEAD` and removes
  the new module, then verifies the manifest's *original* hashes. This is the correct
  "remove the patch entirely" path after several re-installs (the newest backup contains
  the previous **patched** build, not the original hand port).
- Verified on a throwaway clone: dry-run → install → verify → re-install ("already
  patched") → rollback to pre-install backup → pristine verify → re-install →
  `--to-pristine` → pristine verify.
- Patch integrity: forward apply and reverse apply on a pristine `HEAD` extraction both
  produce exact manifest hashes.

## 3. Residual risks / deliberate limits (documented, not bugs)

1. Answers longer than 8000 chars render as **card head + continuation messages**
   (deliberate: one bounded card is the reference behaviour; continuation avoids losing
   text). Normal answers stay one card.
2. If Feishu accepts a card create but the acknowledgement is lost, the adapter falls back
   to text for that frame; no client-side idempotency key exists on Feishu's message API,
   so a rare duplicate-looking frame is possible on transport ambiguity.
3. Classic card JSON 1.0 + whole-card PATCH (no CardKit 2.0 streaming, no chat-list
   summary preview) — see `README.md` deviations.
4. Plugin-transformed final responses that arrive **after** streaming still use the
   upstream "edit the streamed message" reconciliation; on a card target that edit is a
   text update and fails, so the transformed answer is delivered as a normal final text
   message. Pre-existing upstream path; not part of the card transport.
5. Approval/clarify interactions remain exactly upstream behaviour (per request): the
   streaming card stays open across an approval prompt and continues in the same card
   afterwards; no extra "waiting for approval" status line is added.

## 4. Re-run everything

```sh
python3 tests/test_card_stream.py                       # 19/19
cd ~/.hermes/hermes-agent && PYTHONPATH=. \
  .venv/bin/python <repo>/tests/test_card_integration.py # 17/17
./tools/verify_patch.py                                  # pinned patch sha/hunks OK
./tools/verify_patch.py --version bf0203a7               # v0.22.5 variant OK
./tools/verify_patch.py --check-dir ~/.hermes/hermes-agent
```

## 5. Follow-up after the live logs audit (v0.22.5, 2026-10-25)

`host-a` was upgraded by another agent to Hermes v0.22.5 while this port was live. Logs from
10-24 showed the exact failure this audit had warned about, at production scale:

- 21:52:01 a card PATCH hit Feishu `230020 (Update the single messages too frequently)`;
  the consumer disabled the card transport for the rest of that **4920-second** turn.
  The card kept only a 120-char preamble + footer, while the complete 1723-char answer
  was delivered as a separate `post` message at 23:01:35 — two partial versions of one
  reply, plus a stream of `⏳ Working — N min …` and `✍️ Writing …` bubbles from the
  fallback path.
- Even on healthy card turns the gateway's 3-minute heartbeat posted its own
  `⏳ Working — 3 min — iteration …` bubble alongside the card.
- Pure thinking/generation phases showed no elapsed time in the card (the old heartbeat
  only refreshed during a tool wait), and tool status had no argument detail.

Implemented in `patches/bf0203a7` (P0–P3):

1. **P0 — rate-limit resilience**: 230020 triggers adapter backoff retries
   (1.2s/2.4s/4.8s, final frame one extra 3s attempt) instead of an immediate downgrade;
   the hard PATCH floor rose from 0.25s to 1.0s; if the final frame still fails after
   retries the partial card is recalled, so the legacy full-text answer is never paired
   with a stale card.
2. **P1 — richer status**: elapsed time in every non-final state, ` · 第 N 轮` from the
   v0.22.5 step callback, `最后：terminal: <arg preview>` (40 chars), and the 15s
   heartbeat now refreshes all states.
3. **P2 — one-card presentation**: while a live card exists, gateway
   `⏳ Working — N min …` heartbeats and routine `✍️ Writing …` status callbacks are
   suppressed (warnings still send their own message).
4. **P3 — footer**: the installer appends `latency` to the Feishu footer fields
   (`model · context_pct · cache · tok_s · latency`).

Evidence: 19/19 unit + 17/17 integration tests, two live probes (the second asserts the
real card contains `第 7 轮` and the tool detail), 46/46-hunk forward+reverse patch
integrity, gateway restart at 00:19:11 CST (PID <redacted>), and a startup log confirming the
card transport. Full details in `VERIFICATION.md` §5.4.

## 6. Presentation refresh — reference 2026.10.6 (2026-10-25)

The reference repository updated its layout; this patch now matches it as closely as the
classic-card transport allows:

- single phase status line at the **top** of the card (no second narration line);
- compact grey `🧾 运行详情` line: per-tool counts (`💻terminal×2、📖read file×1`), error
  count, short reasoning preview; reasoning is hidden once the final answer exists;
- final answer followed by `> ✅ 完成 · N 个工具 · [M 个错误] · elapsed` (or
  `⚠️ 完成但有错误`); tool-only turns use that summary as the body;
- final `config.summary` chat-list preview (answer snippet, `✅ 已回复` fallback);
- footer metrics kept (`model · context_pct · cache · tok_s · latency`).

Why not the collapsible panel: the reference uses CardKit 2.0 `collapsible_panel` element
updates, while this transport sends classic interactive cards and PATCHes the whole card.
A live API probe showed the classic card accepts the element but the Feishu client renders
an “升级客户端” placeholder instead of the panel, so the same information is shown as a
compact grey note. True collapsing would require migrating the transport to CardKit 2.0
element APIs — a larger change, recorded as a limitation rather than shipping a
non-rendering element.

Also carried forward: 230020 backoff/no-downgrade, 1.0s PATCH floor, stale-card discard,
heartbeat/`Writing` bubble suppression while the card is active, footer latency.
Evidence: 19/19 unit + 17/17 integration tests, live probe on the installed build
(`mid details/status: OK`, `final summary/details: OK`), fleet rollout in
`VERIFICATION.md` §5.6.

## 7. Clarify-as-card + final-fold (from the 16:18–16:33 host-a logs)

The next real turn exposed two bugs the earlier layout could not fix:

1. **Clarify prompt outside the card**: the Feishu adapter had no `send_clarify`
   override, so the base text fallback posted the question + numbered options as a
   `post` bubble beside the streaming card. Fixed with a native clarify card: one button
   per choice + Other + Skip, card-action handler resolving
   `tools.clarify_gateway.resolve_gateway_clarify`, inline "already selected" response,
   and a `retire_clarify_card` hook for timeout/typed endings. Open-ended/multi-select
   prompts are a card asking for a typed reply (the gateway text intercept still resolves
   them).
2. **Duplicate final card**: the gateway's stale-finalize reconciliation called
   `edit_message(content, finalize=True)` on the interactive card **without card
   metadata**; the adapter tried a text PUT, Feishu returned
   `[230054] This operation is not supported for this message type`, and the gateway then
   sent the complete answer through the normal final path — creating a second card.
   Fixed in `edit_message`: when the target is the known live card and the content is a
   final answer, fold it into the card (final template/body) instead of the text path.

Evidence: 19/19 unit + 20/20 integration tests (new clarify card, clarify resolution and
final-fold tests); live `host-a` clarify probe (`send_clarify` card with staging/prod/Other/
Skip, recalled) and card probe OK; fleet rollout in `VERIFICATION.md` §5.7.

## 8. Final-card layout redesign (from the 17:39 card)

The first final card after the clarify rollout was still cramped: the single grey
`🧾 运行详情` note combined tool counts, errors **and** a markdown reasoning table
(`💭 | **82** | ...`), clipping into one long unreadable line. Redesigned final layout:

- answer body (pure text, no summary crammed into it);
- `hr`;
- separate completion blockquote `> ✅ 完成 · N 个工具 · [M 个错误] · elapsed`;
- `div` **two-column fields** block: `🔧 工具` / `⏱ 耗时` as short fields, then
  `主要工具` (top 8 counts) and `⚠️ 错误` as full-width rows;
- `hr` + grey footer metrics note.

Reasoning is no longer rendered into the card (it was the crowding source). The
structured `stats` dict is embedded in every streaming payload, so the stale-finalize /
final-fold edit path renders the same final layout and summary — no second card and no
regression to the old cramped note. Tool-only turns use the summary block as the first
element instead of an empty-body placeholder.

Evidence: 19/19 unit + 20/20 integration tests; live `host-a` card probe
(`mid details/status: OK`, `final summary/details: OK` with structured fields); fleet
rollout in `VERIFICATION.md` §5.8.

## 9. Duration dedupe + small-turn details threshold (from the 17:52/17:54 cards)

The next real cards confirmed two remaining issues (also flagged by an independent
reviewer):

1. elapsed time appeared three times — completion summary `2m29s`, details `⏱ 2m29s`,
   footer latency `2m30s` — with a one-second mismatch;
2. 1–2 tool turns still rendered a full details block, which is pure noise.

Fixes:

- completion summary carries only outcome + tool/error counts (no duration);
- final details block renders only when `tool_total >= 3` or an error exists, and its
  fields are just `主要工具` and `⚠️ 错误` (no duplicated count/duration);
- footer latency is the single duration on the card;
- reasoning capture/rendering and the dead `DETAILS_*_HEADER` constants were deleted
  (streaming frames no longer carry `💭` either).

Evidence: 20/20 unit + 20/20 integration tests; live `host-a` probe with 3 tools
(`> ✅ 完成 · 3 个工具`, `主要工具` details, footer `3m12s` once); fleet rollout in
`VERIFICATION.md` §5.9.

## 10. Known follow-ups (tables, long continuations)

The 17:52 card body contained a markdown pipe table, which classic Feishu cards render
as raw `| ... |` text; and >8000-char answers still deliver their tail as a normal
message outside the card. Both are documented deviations (`VERIFICATION.md` §6) and are
not changed in this build: auto-converting pipe tables to card fields/columns and
sending the continuation as a second card are the next candidates.

## 11. P0 follow-through: tables, continuation cards, transient notices

The 5-day audit (35 turns / 18 user messages) found three remaining presentation gaps
that this round fixes:

1. **pipe tables** — `body_to_card_elements()` detects pipe tables outside fenced code
   and renders ≤4 columns as `column_set`, wider tables as a header + bullet list. The
   raw `| ... |` text is gone (probe: table values + headers render in the card).
2. **long continuations** — `_card_split_body()` overflow is chunked by
   `_card_tail_chunks()` (≤6000 chars) and each chunk is sent as a final card titled
   `<title> · 续 N/M`; no loose text tail. Test asserts the tail arrives as an
   interactive card and zero text sends occur.
3. **transient diagnostics** — `_status_callback_sync()` forwards warning/diagnostic
   statuses to `StreamConsumer.on_card_notice()` → `CardStreamTurn.on_notice()`, so
   provider retries/redirects render in the card status line instead of one text bubble
   per retry. Routine progress still folds silently; tool/answer progress clears the
   notice. A normal-lane final fallback folds into the still-open live card
   (`adapter._fold_final_into_live_card`) with a red header for error bodies, so a
   failed turn cannot leave “half-written card + error bubble”.

Evidence: 25/25 unit + 22/22 integration tests; live `host-a` probe with a 3-tool turn + a
pipe table + a provider notice (`mid details/status: OK`, `final summary/details: OK`);
fleet rollout in `VERIFICATION.md` §5.10.

## 12. P1/P2: capsules, buttons, CardKit 2.0, observability, auto-derive

P1:

- **Header capsule**: classic header title gets `· 运行中 / · 完成 / · 出错` and red
  template on error; CardKit 2.0 uses `text_tag_list` + subtitle for the same state.
- **Tool error reason**: `tool.completed` detail (first line, ≤90 chars) is appended to
  the `⚠️ 错误` field instead of a bare “执行失败”.
- **Multi-select clarify**: toggle buttons (✅/○) + Submit (`✅ 提交（N）`), resolving a
  JSON label array; typed “1,3” remains supported.
- **Slash-confirm buttons**: `send_slash_confirm` renders Approve Once / Always /
  Cancel and resolves via `tools.slash_confirm.resolve()`; the old `/new` prompt only
  had text fallback.
- **Final retry**: 0/1.5/3/6s retries; keeps the card for the normal-final fold/recall
  path instead of deleting it first.

P2:

- **CardKit 2.0 optional transport** (`cardkit_streaming`): entity create/send,
  `card_element.content` status/body streaming, settings close, final full-card swap
  with native table + collapsible panel. Live host-a probe: every call `code=0`; the
  current Feishu client still renders the 2.0 “upgrade client” placeholder, so it is
  off by default (classic fallback).
- **Observability**: best-effort JSONL metrics for send/patch/rate-limit/fold/finalize/
  cardkit events + `tools/card_health.py`; no message content or chat ids recorded.
- **Auto-derive**: `tools/derive_build.py` uses a git worktree + `patch --fuzz=3`,
  materializes pristine/patched overlays and calls `make_patch_variant.py`; tested on
  bf0203a7 (reproduced the same patch sha) and reports conflicts with `.rej` paths.

Evidence: 29/29 unit + 24/24 integration tests; live classic probe OK; live CardKit
adapter probe (create/send/content×2/close/final/footer all success); fleet rollout in
`VERIFICATION.md` §5.11.
