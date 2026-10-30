"""Gateway streaming consumer — bridges sync agent callbacks to async platform delivery.

on_delta() queues deltas from the agent's worker thread; the async run() task buffers,
rate-limits and progressively edits one platform message (send, then editMessageText;
draft/native transports are optional per adapter).
Credit: jobless0x (#774, #1312), OutThisLife (#798), clicksingh (#697).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import inspect
import logging
import queue
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from gateway.platforms.base import BasePlatformAdapter as _BasePlatformAdapter
from gateway.platforms.base import _custom_unit_to_cp
from gateway.config import (
    DEFAULT_STREAMING_EDIT_INTERVAL as _DEFAULT_STREAMING_EDIT_INTERVAL,
    DEFAULT_STREAMING_BUFFER_THRESHOLD as _DEFAULT_STREAMING_BUFFER_THRESHOLD,
    DEFAULT_STREAMING_CURSOR as _DEFAULT_STREAMING_CURSOR)
from gateway.response_filters import (
    is_intentional_silence_response as _is_intentional_silence_response,
    is_partial_silence_marker as _is_partial_silence_marker)
from gateway.stream_consumer_fences import ensure_closed_code_fences
from gateway.stream_consumer_transport import StreamTransportMixin
from gateway.stream_consumer_fallback import StreamFallbackMixin
from gateway.stream_consumer_think import StreamThinkFilterMixin

logger = logging.getLogger("gateway.stream_consumer")

# Queue sentinels (see _drain_queue()).  Bare: _DONE, _NEW_SEGMENT (finalize, start a
# fresh message), _REOPEN_SEED (EAGER native re-seed after a clarify answer — WeCom
# typing is driven by the seed frame; lazy re-seed measured 48s of dead air).  Tuples:
# (_COMMENTARY, text); (_TOOL_PROGRESS, line) native-bubble overlay; (_CARD_TOOL, kind,
# tool_name) / (_CARD_COMMENT, text) streaming-card status events; (_FINAL_TEXT, text)
# authoritative final_response incl. post-stream augmentation, queued just before _DONE;
# (_FLUSH, threading.Event) barrier; (_APPROVAL_BOUNDARY, future, cancelled_flag).
_DONE = object()
_NEW_SEGMENT = object()
_COMMENTARY = object()
_TOOL_PROGRESS = object()
_CARD_TOOL = object()
_CARD_COMMENT = object()
_CARD_STEP = object()
_CARD_NOTICE = object()
_FINAL_TEXT = object()
_FLUSH = object()
_APPROVAL_BOUNDARY = object()
_REOPEN_SEED = object()
_FUTURE_TYPES = (asyncio.Future, concurrent.futures.Future)

# Boundary finalize text when nothing has accumulated yet (overridable per boundary).
_DEFAULT_BOUNDARY_PLACEHOLDER = "⏸ 等待审批中..."


def _is_card_rate_limit(error: str) -> bool:
    """Feishu per-message update frequency limit (transient, retryable)."""
    text = str(error or "").lower()
    return "230020" in text or "frequency limit" in text or "too frequently" in text


@dataclass
class StreamConsumerConfig:
    """Runtime config for a single stream consumer instance."""
    edit_interval: float = _DEFAULT_STREAMING_EDIT_INTERVAL
    buffer_threshold: int = _DEFAULT_STREAMING_BUFFER_THRESHOLD
    cursor: str = _DEFAULT_STREAMING_CURSOR
    buffer_only: bool = False
    # >0: final goes out as a fresh message once the preview has been visible this
    # long (timestamp reflects completion); 0 = always edit in place.
    # This makes the platform's visible timestamp reflect completion time instead of first-token time for
    # long-running responses (e.g. reasoning models that stream slowly). Ported from
    # openclaw/openclaw#72038. The gateway enables this selectively per-platform.
    fresh_final_after_seconds: float = 0.0
    # "auto"/"draft": native drafts when adapter+chat support it, else "edit"
    # (progressive editMessageText).  "off" is handled by the gateway.
    transport: str = "edit"
    chat_type: str = ""  # originating chat type; gates platform-specific drafts


@dataclass
class _Tick:
    """Everything one drain of the queue decided."""
    got_done: bool = False
    got_segment_break: bool = False
    got_flush: bool = False
    flush_event: Any = None
    got_reopen_seed: bool = False
    approval_boundary: Optional[tuple] = None  # (future, cancelled_flag)
    commentary_text: Optional[str] = None
    # A streaming-card status/tool/narration event arrived this tick.
    card_dirty: bool = False
    # Set by _push_update for _finalize_turn / _end_segment.
    update_visible: bool = False
    draft_final_fresh_send: bool = False

    @property
    def is_interim(self) -> bool:
        """Mid-stream tick: not finalizing, not a segment break, no commentary."""
        return not self.got_done and not self.got_segment_break and self.commentary_text is None


class GatewayStreamConsumer(StreamTransportMixin, StreamFallbackMixin, StreamThinkFilterMixin):
    """Async consumer that progressively edits a platform message with streamed tokens.
    Usage: ``agent.stream_delta_callback = consumer.on_delta``; ``create_task(consumer.run())``;
    after the agent finishes ``consumer.finish()`` then ``await task`` for the final edit."""

    _MAX_FLOOD_STRIKES = 3  # consecutive flood failures before edits are disabled

    # Class-wide monotonic draft-id counter (Telegram animates a draft only when the
    # same non-zero draft_id is reused).  RANDOM seed: draft_id keys the relay
    # connector's sealed-stream tombstones, which outlive this process — a replayed id
    # is answered out of the OLD tombstone and dropped.  49 bits stays inside JS 2^53.
    _draft_id_counter: int = secrets.randbits(49)

    def __init__(
        self,
        adapter: Any,
        chat_id: str,
        config: Optional[StreamConsumerConfig] = None,
        metadata: Optional[dict] = None,
        on_new_message: Optional[callable] = None,
        on_before_finalize: Optional[Callable[[], Any]] = None,
        initial_reply_to_id: Optional[str] = None,
        run_still_current: Optional[Callable[[], bool]] = None):
        self.adapter = adapter
        self.chat_id = chat_id
        self.cfg = config or StreamConsumerConfig()
        self.metadata = metadata
        # Hooks (exceptions swallowed): on_new_message per fresh content bubble (next
        # tool-progress bubble goes BELOW it); on_before_finalize once (pause typing).
        self._on_new_message = on_new_message
        self._on_before_finalize = on_before_finalize
        self._initial_reply_to_id = initial_reply_to_id
        self._turn_id = str(uuid.uuid4())  # keys send_stream_frame() per concurrent consumer
        # Returns False after /new or /stop; run() then abandons the stream.
        self._run_still_current = run_still_current or (lambda: True)
        # Whether this consumer is fed the final reply's stream deltas. A consumer built only to
        # relay interim commentary (text streaming off, ``display.interim_assistant_messages`` on)
        # never receives the final's deltas, so the duplicate-risk diagnostic in
        # ``_run_agent_mark_streamed_delivery`` must not fire for it (#105341). Default True: every
        # other construction site (incl. the proxy path) creates consumers only when streaming is on.
        self.stream_deltas_enabled = True
        # Only platforms needing an explicit finalize call (DingTalk AI Cards) force a
        # redundant final edit; ``is True`` keeps MagicMock adapters out.
        self._adapter_requires_finalize = getattr(adapter, "REQUIRES_EDIT_FINALIZE", False) is True
        # Telegram bounds edit retries at 5s; a fallback must not wait longer.
        self._max_fallback_flood_retry_seconds = 5.0

        self._queue: queue.Queue = queue.Queue()
        # Every real preview id on screen this response (fresh-final deletes them all);
        # the per-segment set holds only the active segment so failure recovery never
        # deletes an earlier finalized preamble/commentary.
        # Wall-clock timestamp (time.monotonic) when ``_message_id`` was first assigned from a successful
        # first-send. Used by the fresh-final logic to detect long-lived previews whose edit timestamps
        # would be stale by completion time. Ported from openclaw/openclaw#72038.
        self._preview_message_ids: set[str] = set()
        self._already_sent = False
        self._edit_supported = True  # False once progressive edits stop working
        self._last_edit_time = 0.0
        self._last_edit_overflowed = False  # last _send_or_edit split into continuations
        self._flood_strikes = 0
        self._current_edit_interval = self.cfg.edit_interval  # adaptive backoff
        self._delivered_commentary_texts: list[str] = []
        self._delivered_segment_texts: list[str] = []  # finalized text per past segment
        self._in_think_block = False  # think-tag filter state (mirrors CLI _stream_delta)
        self._think_buffer = ""
        self._before_finalize_notified = False
        self._reset_message_state()

        # Transports, resolved in run().  Draft: animated frames via adapter.send_draft;
        # the final still uses first-send; the first failure disables drafts.  Native
        # (WeCom msgtype "stream"): the ONLY channel — any failure falls back to edit/send.
        self._use_draft_streaming = False
        self._draft_id: Optional[int] = None
        self._draft_failures = 0
        # TERMINAL authorization refusal for THIS RUN (see _send_draft_frame).
        # Per-run state, constructed fresh each turn, so a refusal can never
        # mute a healthy destination on a later turn.
        self._egress_declined = False
        self._use_native_streaming = False
        self._native_stream_opened = False  # seed sent: bubble open, zero content
        self._native_last_pushed_len = 0    # throttle under WeCom's 30 frames/min
        # Boundary state from close_for_approval_prompt() (boundaries are processed
        # serially).  reopen=True (clarify) keeps native enabled so post-prompt output
        # re-opens a fresh stream; approval degrades to send().
        self._boundary_placeholder = _DEFAULT_BOUNDARY_PLACEHOLDER
        self._boundary_reason = "Approval"
        self._boundary_reopen = False
        # Reopen requested but nothing re-seeded: got_done must not open a stream just
        # to emit a lone "✅"; an EAGER re-seed opened a bubble that got_done MUST close.
        self._awaiting_reopen_after_boundary = False
        self._reopen_seeded_eagerly = False

        # Streaming-card transport (Feishu, openclaw-feishu-patch parity): ONE bounded
        # interactive card per turn.  The adapter is probed in _start_transports(); the
        # card state machine (body + tool/narration status) is rendered into
        # metadata["hermes_card_stream"] on every send/edit.
        self._use_card_streaming = False
        self._card_turn: Any = None  # plugins.platforms.feishu.card_stream.CardStreamTurn
        self._card_last_send_ts = 0.0
        self._card_last_status_sig = ""
        self._card_last_payload_sig = ""
        self._card_pending_sep = False
        self._card_update_interval = max(0.2, float(getattr(self.cfg, "edit_interval", 0.8) or 0.8))
        self._card_min_patch_interval = 1.0  # Feishu per-message updates are stricter than the app QPS
        self._card_pending = False
        self._last_card_error = ""

    def _reset_message_state(self) -> None:
        """Per-message (segment) state: fresh at construction and after each segment break."""
        self._message_id: Optional[str] = None
        self._message_created_ts: Optional[float] = None  # fresh-final age
        # ``_stream_ledger`` mirrors ``_accumulated`` but is NOT truncated when
        # overflow splits seal head chunks (reconcilable turn-final payload).
        self._accumulated = self._stream_ledger = ""
        self._last_sent_text = ""    # skip redundant edits
        self._fallback_final_send = False
        self._fallback_prefix = ""
        # Fallback sends only the missing tail after a partial overflow delivery.
        self._fallback_preserve_partial_messages = False
        self._segment_preview_message_ids: set[str] = set()
        # Tool-progress overlay (native only): shown in the bubble until text arrives.
        self._tool_progress_lines: list[str] = []
        self._tool_progress_active: bool = False
        self._clear_turn_final_flags()

    def _clear_turn_final_flags(self) -> None:
        """Reset every turn-final delivery flag to "nothing delivered yet".
        ``_delivered_final_text`` is the cleaned turn-final payload the gateway compares to
        the completed final_response before trusting the flags (a successful finalize edit
        may carry a stale preview); None = legacy trust.  A payload-less
        ``_turn_split_delivery`` must NOT inherit legacy trust; ``_delivery_ambiguous`` (a
        full-final send timed out but MAY have landed) is the only case that does."""
        # #29346: a tool/segment boundary means what we delivered was an interim preamble, not the final
        # answer — clear the flags so a premature setter can't fool the gateway. Safe: got_done returns
        # before any reset, and run.py reads these only after the consumer task exits.
        self._final_response_sent = False
        self._final_content_delivered = False  # content landed even if the cosmetic edit failed
        self._delivered_final_text: Optional[str] = None
        self._turn_split_delivery = False
        # True when a full-final send timed out in a way that MAY have reached the platform
        # (``_send_empty_fallback_final`` → "ambiguous"). The only case where a payload-less delivery flag
        # keeps legacy trust in ``delivered_final_matches`` (#95382 tightening) — re-sending there risks a
        # duplicate rather than recovering a loss.
        self._delivery_ambiguous = False

    def _stream_is_message(self) -> bool:
        """Whether THIS chat's transport treats the stream as the message: per-chat probe
        first (a relay adapter's class attribute only reflects its primary identity), else
        the legacy attribute; both on the CLASS (MagicMock-safe)."""
        probe = getattr(type(self.adapter), "stream_is_message_for_chat", None)
        if not callable(probe):
            return getattr(self.adapter, "draft_stream_is_message", False) is True
        try:
            return probe(self.adapter, str(self.chat_id)) is True
        except Exception:
            return False

    @property
    def accepts_tool_progress(self) -> bool:
        """True when tool progress can be overlaid in-stream (native bubble or streaming card)."""
        return self._use_native_streaming or self._use_card_streaming

    @property
    def accepts_card_stream_events(self) -> bool:
        """True when the turn renders as ONE streaming card (status/tool/narration fold-in)."""
        return self._use_card_streaming

    def on_tool_progress(self, line: str) -> None:
        """Thread-safe: overlay a tool-progress line in the native bubble until the next delta."""
        if line:
            self._queue.put((_TOOL_PROGRESS, line))

    def on_card_tool(self, event_type: str, tool_name: Optional[str] = None, detail: str = "",
                     is_error: bool = False) -> None:
        """Thread-safe: queue a tool lifecycle event for the streaming card status."""
        if not self._use_card_streaming:
            return
        self._queue.put((_CARD_TOOL, str(event_type or ""), str(tool_name or ""), str(detail or ""),
                         bool(is_error)))

    def on_card_notice(self, text: str) -> bool:
        """Thread-safe: render a transient gateway diagnostic in the card status line.

        Returns True when the card consumed it; False lets the caller fall back to the
        ordinary status-bubble path.
        """
        if not self._use_card_streaming or not str(text or "").strip():
            return False
        self._queue.put((_CARD_NOTICE, str(text)))
        return True

    def on_card_step(self, iteration: int) -> None:
        """Thread-safe: agent iteration counter (kept for wiring compatibility)."""
        if not self._use_card_streaming:
            return
        try:
            value = int(iteration)
        except (TypeError, ValueError):
            return
        if value > 0:
            self._queue.put((_CARD_STEP, value))

    @property
    def card_streaming_active(self) -> bool:
        """True while this turn is still rendering into a live streaming card."""
        return bool(self._use_card_streaming and self._card_turn is not None)

    def on_card_commentary(self, text: str) -> None:
        """Thread-safe: queue mid-turn narration for the card's single ticker line."""
        if not self._use_card_streaming or not str(text or "").strip():
            return
        self._queue.put((_CARD_COMMENT, str(text)))

    def _compose_frame_content(self) -> str:
        """Native frame content: text, with any tool-progress lines below a rule."""
        progress = "\n".join(self._tool_progress_lines)
        return "\n\n---\n".join(p for p in (self._accumulated, progress) if p)

    def _metadata_for_send(self, *, final: bool = False, expect_edits: bool = False) -> dict | None:
        """Per-send metadata.  ``final`` → notify=True (Mattermost treats notify-worthy sends
        as final when a broken thread root may fall back flat); ``expect_edits`` keeps
        editable previews on Telegram's legacy send path."""
        meta = dict(self.metadata) if self.metadata else {}
        if self._initial_reply_to_id:
            meta["reply_to_message_id"] = self._initial_reply_to_id
        if expect_edits:
            meta["expect_edits"] = True
        if final:
            meta["notify"] = True
        return meta or None

    # Read-only views for the gateway (flag semantics: see _clear_turn_final_flags).
    already_sent = property(lambda self: self._already_sent)
    final_response_sent = property(lambda self: self._final_response_sent)
    message_id = property(lambda self: self._message_id)
    final_content_delivered = property(lambda self: self._final_content_delivered)

    async def _notify_before_finalize(self) -> None:
        """Run the pre-finalize hook exactly once, swallowing hook errors."""
        if self._before_finalize_notified:
            return
        self._before_finalize_notified = True
        if self._on_before_finalize is not None:
            with contextlib.suppress(Exception):
                result = self._on_before_finalize()
                if inspect.isawaitable(result):
                    await result

    def _append_accumulated(self, text: str) -> None:
        """Append to the live buffer and the split-stable stream ledger."""
        if not text:
            return
        if self._tool_progress_lines:  # real text overwrites the overlay
            self._tool_progress_lines.clear()
            self._tool_progress_active = False
        if self._use_card_streaming and self._card_pending_sep:
            # Tool/segment boundary in card mode: keep ONE body, separate blocks with a
            # blank line instead of resetting into a second message.
            self._card_pending_sep = False
            if self._accumulated:
                text = "\n\n" + text
        self._accumulated += text
        self._stream_ledger += text

    def _mark_skip_redundant_finalize(self) -> None:
        """Mark the turn final as delivered by a prior mid-stream edit.  Records what was
        ACKED on the wire, not ``_accumulated``: a throttled stream's last ack may be an
        older cursor-suffixed preview, which must not suppress the corrective send."""
        acked = self._last_sent_text or self._accumulated
        if self.cfg.cursor and acked.endswith(self.cfg.cursor):
            acked = acked[: -len(self.cfg.cursor)]
        self._mark_final_delivered(record=acked)

    def _mark_final_delivered(self, record: Optional[str] = None) -> None:
        """Set both turn-final flags; ``record`` also records the delivered payload."""
        self._final_response_sent = True
        # Only claim final delivery if the sealed chunks and final tail actually landed. ``_already_sent``
        # may be True from prior progress/fallback state (#10748).
        # The final clean-up edit failed, but the complete answer is already visible from the last streaming
        # frame (usually with only the cursor still stuck on screen). Mark the content delivered so the
        # gateway suppresses its normal full final send; otherwise users see the same long answer twice when
        # Telegram/Discord rate-limit this cosmetic final edit (#36965, #25349).
        self._final_content_delivered = True
        if record is not None:
            self._record_turn_final_payload(record)

    def _display_payload(self, text: str) -> str:
        """Normalize like ``_send_or_edit`` output: directive strip + fence close + strip."""
        return ensure_closed_code_fences(self._clean_for_display(text or "")).strip()

    def _record_turn_final_payload(self, text: str) -> None:
        """Record what the user actually saw as this turn's final answer.  On a split ``text``
        is only the trailing chunk, so the un-truncated ``_stream_ledger`` is recorded — else
        the gateway sees a mismatch and re-sends an answer the user already received."""
        if self._turn_split_delivery and self._stream_ledger:
            text = self._stream_ledger
        self._delivered_final_text = self._display_payload(text)

    def delivered_final_matches(self, final_text: str) -> Optional[bool]:
        """Tri-state reconcile of the recorded turn-final payload against ``final_text`` (a
        *successful* finalize edit can still carry a stale preview, so call success alone
        must not confirm delivery).  True: recorded payload (or an earlier segment /
        commentary) matches.  False: payload differs, or payload-less split.  None: nothing
        recorded on a legacy/ambiguous path (caller trusts flags)."""
        target = self._display_payload(final_text)
        if not target:
            return None
        if self._delivered_final_text is not None:
            # A segment break / commentary may have delivered it under another record.
            return (self._delivered_final_text.strip() == target
                    or self.has_delivered_text(final_text))
        if self._turn_split_delivery:
            return False
        # No recorded payload: judge against the FINAL content, not the flag.
        # ``_already_sent`` gates the match: draft frames set ``_last_sent_text`` but
        # deliberately not ``_already_sent``.
        # #95382 / #98552 class fix: a delivery flag with NO recorded payload must still be judged against
        # the FINAL content, not trusted blindly. Every internal flag-setting site records a payload; a
        # record-less consumer whose visible/streamed text does not contain the completed response has
        # demonstrably NOT delivered it (first-edit prefix, mid-stream truncation) — the flag alone must not
        # suppress the corrective send. ``_already_sent`` gates the visible-text match: draft frames set
        # ``_last_sent_text`` for dedupe but are ephemeral (they deliberately do not set ``_already_sent``),
        # so draft-only visibility must not count as durable delivery.
        if self._already_sent and self.has_delivered_text(final_text):
            return True
        # Only a timed-out full-final send that MAY have landed keeps legacy trust.
        return None if self._delivery_ambiguous else False

    def has_delivered_text(self, text: str) -> bool:
        """Return True if *text* was already delivered as visible chat content."""
        target = self._clean_for_display(text or "").strip()
        seen = (self._visible_prefix(), *self._delivered_commentary_texts,
                *self._delivered_segment_texts)
        return bool(target) and any(sent.strip() == target for sent in seen)

    def has_durably_delivered_text(self, text: str) -> bool:
        """``has_delivered_text`` restricted to deliveries that outlive the turn: commentary and
        finalized segments always count; the visible prefix only once ``_already_sent`` (a draft frame
        sets ``_last_sent_text`` but is ephemeral — a failed finalize send after it must still fall
        back to the gateway's real final send, same gate as ``delivered_final_matches``)."""
        target = self._clean_for_display(text or "").strip()
        seen = [*self._delivered_commentary_texts, *self._delivered_segment_texts]
        if self._already_sent:
            seen.append(self._visible_prefix())
        return bool(target) and any(sent.strip() == target for sent in seen)

    def on_segment_break(self) -> None:
        """Finalize the current stream segment and start a fresh message."""
        self._queue.put(_NEW_SEGMENT)

    def close_for_approval_prompt(
        self, placeholder: str | None = None, reason: str = "Approval", reopen: bool = False,
    ) -> asyncio.Future:
        """Queue an interaction boundary (approval / clarify prompt) from sync context.
        run() finalizes the current native stream (``placeholder`` when empty), then per
        ``reopen``: False (approval; unbounded waits) degrades to one send() at got_done;
        True (clarify) keeps native enabled so post-prompt output re-opens a fresh stream.
        Returns (Future, cancelled_flag); the Future resolves True once processed
        (cancelled_flag is legacy, no longer read).  Without native streaming returns a
        bare, already-resolved Future."""
        loop = None
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
        boundary_future = loop.create_future() if loop else concurrent.futures.Future()
        if not self._use_native_streaming:
            boundary_future.set_result(True)
            return boundary_future
        # Instance attributes are race-free: boundaries are processed one at a time.
        self._boundary_placeholder = placeholder or _DEFAULT_BOUNDARY_PLACEHOLDER
        self._boundary_reason = reason or "Approval"
        self._boundary_reopen = bool(reopen)
        cancelled_flag = {"cancelled": False}
        self._queue.put((_APPROVAL_BOUNDARY, boundary_future, cancelled_flag))
        return boundary_future, cancelled_flag

    def on_commentary(self, text: str) -> None:
        """Queue a completed interim assistant commentary message."""
        if text:
            self._queue.put((_COMMENTARY, text))

    def flush_pending_sync(self, timeout: float = 5.0) -> bool:
        """Block the agent worker thread until everything queued so far is delivered:
        ``(_FLUSH, Event)`` barrier — run() drains earlier items (FIFO), finalizes the
        segment, sets the event.  False on timeout (consumer task may not be running)."""
        evt = threading.Event()
        try:
            self._queue.put((_FLUSH, evt))
        except Exception:
            return False
        return evt.wait(timeout=max(0.0, float(timeout)))

    def _reopen_seed_pending(self) -> bool:
        """Native stream, reopen requested after a boundary, nothing open yet."""
        return (self._use_native_streaming and self._awaiting_reopen_after_boundary
                and not self._native_stream_opened)

    def request_reopen_seed(self) -> None:
        """Thread-safe: request an EAGER native re-seed after a clarify answer.  No-op unless
        reopen-pending, so a stray call can't open a spurious bubble mid-stream or on approval."""
        if self._reopen_seed_pending():
            self._queue.put(_REOPEN_SEED)

    def _notify_new_message(self) -> None:
        """Fire the on_new_message callback, swallowing any errors."""
        try:
            if self._on_new_message is not None:
                self._on_new_message()
        except Exception:
            logger.debug("on_new_message callback error", exc_info=True)

    @staticmethod
    def _signal_flush(flush_event) -> None:
        """Wake a thread blocked in flush_pending_sync(), swallowing errors.  Every loop path
        that consumed a ``_FLUSH`` barrier (incl. early ``continue``) must call this; a
        missed set stalls the caller for the full timeout."""
        if flush_event is not None:
            with contextlib.suppress(Exception):
                flush_event.set()

    def _reset_segment_state(self, *, preserve_no_edit: bool = False) -> None:
        if preserve_no_edit and self._message_id == "__no_edit__":
            return
        # Retain the segment's visible text so has_delivered_text still matches.
        finalized = self._clean_for_display(self._last_sent_text).strip()
        if finalized:
            self._delivered_segment_texts.append(finalized)
        # Also clears the final flags: what we delivered was an interim preamble.  Safe:
        # got_done returns before any reset; run.py reads flags after the task exits.
        self._reset_message_state()
        # Telegram-shaped drafts: bump draft_id so the next segment animates as a fresh
        # preview below the tool-progress bubbles.  Stream-is-the-message adapters keep
        # ONE stream per turn — a bump there left one frozen message per segment.
        if self._use_draft_streaming and not self._stream_is_message():
            self._bump_draft_id()

    def _bump_draft_id(self) -> None:
        type(self)._draft_id_counter += 1
        self._draft_id = type(self)._draft_id_counter

    async def _handle_approval_boundary(self, boundary_future, cancelled_flag=None) -> None:
        """Serially process an interaction boundary dequeued by run().  The stream is never
        kept open across a prompt: the WeCom finalize ack only confirms server receipt, and
        after a long idle gap the client may stop tracking the stream."""
        _reason = self._boundary_reason or "Approval"
        if self._use_card_streaming:
            # Card mode keeps the card across the prompt: the approval/clarify prompt is
            # its own interactive card and post-answer deltas continue in this card.
            with contextlib.suppress(Exception):
                if isinstance(boundary_future, _FUTURE_TYPES) and not boundary_future.done():
                    boundary_future.set_result(True)
            return
        try:
            boundary_ok = True
            if self._native_stream_opened:
                boundary_ok = await self._finalize_boundary_stream(_reason)
            if self._boundary_reopen:
                # Clarify: keep native enabled (NOT buffer_only); the closed stream
                # makes the next post-prompt delta re-open via the lazy re-seed.  The
                # gap to the "Re-opened native stream" INFO is the typing latency.
                self._close_native_state()
                self._awaiting_reopen_after_boundary = True
            else:
                # Approval: post-approval output goes via one send() at got_done.
                self._degrade_native_to_buffered_send()
            self._reset_segment_state()
            if self._boundary_reopen:
                logger.info("[latency] Clarify boundary finalized, awaiting first "
                            "post-answer delta to re-seed (chat=%s, turn=%s)",
                            self.chat_id, self._turn_id)
        except Exception as e:
            logger.warning("%s boundary processing failed: %s", _reason, e)
            boundary_ok = False
        finally:
            with contextlib.suppress(Exception):
                if isinstance(boundary_future, _FUTURE_TYPES) and not boundary_future.done():
                    boundary_future.set_result(boundary_ok)

    async def _finalize_boundary_stream(self, _reason: str) -> bool:
        """Close the open native stream at a boundary; send() the pre-prompt text if that
        fails.  False only when both finalize and the fallback send failed."""
        finalize_text = self._accumulated or self._boundary_placeholder
        try:
            if await self._send_frame(finalize_text, finalize=True):
                logger.debug("%s boundary: finalized stream (chat=%s, turn=%s)",
                             _reason, self.chat_id, self._turn_id)
                return True
        except Exception as e:
            logger.warning("%s boundary: finalize failed: %s", _reason, e)
        # Typing bubble may still show partial content; deliver via send().
        logger.warning("%s boundary: finalize not confirmed, "
                       "falling back to send() for pre-prompt text (chat=%s)",
                       _reason, self.chat_id)
        try:
            if getattr(await self.adapter.send(self.chat_id, finalize_text), "success", False):
                return True
        except Exception as send_err:
            logger.warning("%s boundary: fallback send also failed: %s", _reason, send_err)
        logger.error("%s boundary: both finalize and fallback send failed "
                     "(chat=%s) — pre-prompt text may not have been delivered",
                     _reason, self.chat_id)
        return False

    def on_delta(self, text: str) -> None:
        """Thread-safe callback from the agent's worker thread.  ``None`` signals a tool
        boundary: the current message is finalized and subsequent text goes out as a new
        message below any tool-progress messages."""
        if text:
            self._queue.put(text)
        elif text is None:
            self.on_segment_break()

    def finish(self, final_text: Optional[str] = None) -> None:
        """Signal stream completion.  ``final_text`` is the AUTHORITATIVE completed
        final_response (incl. post-stream augmentation the accumulator never saw); the drain
        loop adopts it as the finalize payload.  Interrupt/error paths call ``finish()`` bare."""
        if final_text is not None:
            self._queue.put((_FINAL_TEXT, final_text))
        self._queue.put(_DONE)

    async def run(self) -> None:
        """Async task that drains the queue and edits the platform message."""
        self._len_fn, self._safe_limit = self._resolve_length_budget()
        await self._start_transports()
        try:
            while True:
                # Session reset (/new, /stop): abandon rather than deliver stale deltas.
                if not self._run_still_current():
                    await self._abandon_native_stream()
                    return
                tick = self._drain_queue()

                # Boundary produces its own finalize and resets state, so it must
                # run before got_done/segment_break processing.
                if tick.approval_boundary is not None:
                    await self._handle_approval_boundary(*tick.approval_boundary)
                    continue
                if tick.got_reopen_seed:
                    await self._eager_reopen_seed()
                    continue

                if tick.got_done:
                    self._flush_think_buffer()
                    # A bare intentional-silence marker (NO_REPLY / [SILENT]): the
                    # gateway's whole-response filter runs too late for a streamed
                    # preview, so retract it here instead of finalizing.
                    if _is_intentional_silence_response(self._clean_for_display(self._accumulated)):
                        await self._suppress_silence_marker()
                        return

                if self._use_card_streaming:
                    # ONE interactive card per turn: answer body + folded tool/narration
                    # status; tool boundaries never split it into a second bubble.
                    await self._card_push_update(tick)
                    if tick.got_done:
                        await self._finalize_turn(tick)
                        return
                    if tick.commentary_text is not None:
                        await self._deliver_commentary(tick.commentary_text)
                    elif tick.got_segment_break:
                        # Mark the paragraph break so the next answer block appends
                        # under the same card body with a blank line, never a new card.
                        await self._end_segment(tick)
                    # A segment break is cumulative in card mode: no message reset.
                    if tick.got_flush:
                        self._signal_flush(tick.flush_event)
                    await self._card_maybe_elapsed_refresh()
                    await asyncio.sleep(0.05)
                    continue

                if self._should_edit(tick) and (
                    self._accumulated or (self._use_native_streaming and self._tool_progress_active)
                ):
                    # Seal first: it clears the message id, so a remainder still over the limit
                    # is split again below. A plain first send would let the adapter split it and
                    # adopt only the LAST chunk as the preview; the next seal then overwrites that
                    # chunk with the head of the whole remainder (duplicated + lost text, #25349).
                    await self._seal_overflow_heads()
                    # Overflow split.  Native streaming bypasses this: the adapter
                    # truncates against the stream protocol's own limit.
                    if not self._use_native_streaming and self._first_send_overflows():
                        if await self._split_first_send(tick):
                            return
                        if self._first_send_overflows():
                            # A head send failed: keep the full text for the fallback final, and
                            # skip the boundary reset below that would clear it. Still yield: the
                            # buffer stays over the debounce threshold, so `continue` alone re-enters
                            # the split on every pass and spins the loop without ever sleeping.
                            self._signal_flush(tick.flush_event)
                            await asyncio.sleep(0.05)
                            continue
                    # The split tail goes out now, so a commentary or tool boundary drained in
                    # this tick still lands after it instead of being dropped.
                    await self._push_update(tick)

                if tick.got_done:
                    await self._finalize_turn(tick)
                    return

                if tick.commentary_text is not None:
                    await self._deliver_commentary(tick.commentary_text)
                if tick.got_segment_break:
                    await self._end_segment(tick)

                # Done last so the waiter unblocks only once everything queued
                # before the barrier is on screen.
                if tick.got_flush:
                    self._signal_flush(tick.flush_event)

                await asyncio.sleep(0.05)  # Small yield to not busy-loop

        except asyncio.CancelledError:
            await self._on_cancelled()
        except Exception as e:
            logger.error("Stream consumer error: %s", e)
        finally:
            self._wake_flush_waiters()

    # ── run() collaborators ─────────────────────────────────────────────

    def _resolve_length_budget(self) -> tuple[Callable[[str], int], int]:
        """Per-chat length function (relay adapters differ per chat, e.g. utf16) + budget.
        isinstance gate: MagicMock auto-attributes aren't callables; test doubles use len."""
        # Shares the guarded ladder with the fallback path: a git pull while the gateway
        # runs can pair new consumer code with an old in-memory adapter lacking
        # message_len_fn_for_chat (#72628), which then falls back to message_len_fn.
        len_fn, _ = self._fallback_len_budget()
        return len_fn, max(500, self._raw_message_limit() - len_fn(self.cfg.cursor) - 100)

    async def _start_transports(self) -> None:
        """Resolve native/draft transport; native wins (adapters declaring it can't edit).
        The empty seed frame shows "typing" before the first token; on failure → edit path."""
        self._use_native_streaming = self._resolve_native_streaming()
        if self._use_native_streaming:
            logger.debug("Stream consumer using native-stream transport (chat=%s)", self.chat_id)
            if await self._try_seed_frame("Native streaming seed frame raised; disabling native",
                                          exc_info=True):
                self._native_stream_opened = True
                self._use_draft_streaming = False
                return
            self._use_native_streaming = False
        if self._resolve_card_streaming():
            self._use_card_streaming = True
            logger.debug("Stream consumer using streaming-card transport (chat=%s)", self.chat_id)
            self._init_card_turn()
            if await self._prewarm_card():
                self._use_draft_streaming = False
                return
            # Prewarm failed: fall back to the ordinary edit transport for this turn.
            logger.info("Streaming-card prewarm failed (chat=%s); falling back to edit transport", self.chat_id)
            self._use_card_streaming = False
            self._card_turn = None
        self._use_draft_streaming = self._resolve_draft_streaming()
        # Native draft streaming: bump the draft_id so the next text segment animates as a fresh preview
        # below the tool-progress bubbles, not over the prior segment's already-finalized draft. This is how
        # we avoid the "inter-tool-call text leak" failure mode openclaw documented in their issue #32535 —
        # each text block becomes its own visible message via the finalize, then a new draft animates for
        # the next one.
        if self._use_draft_streaming:
            self._bump_draft_id()
            logger.debug("Stream consumer using native-draft transport (chat=%s draft_id=%s)",
                         self.chat_id, self._draft_id)

    # ── streaming-card transport (Feishu / openclaw-feishu-patch presentation) ──
    #
    # ONE bounded interactive card per turn: the card is prewarmed at reply start,
    # answer deltas patch its body, tool lifecycle events and narration fold into a
    # compact status block, and the final frame removes the status block and adds the
    # runtime footer note (the adapter receives the whole state as
    # metadata["hermes_card_stream"] and patches the Feishu card in place).

    def _resolve_card_streaming(self) -> bool:
        """Probe the adapter for a cumulative interactive-card stream transport."""
        if not isinstance(self.adapter, _BasePlatformAdapter):
            return False  # test doubles / duck-typed adapters keep the legacy transport
        probe = getattr(type(self.adapter), "supports_card_streaming", None)
        if not callable(probe):
            return False
        try:
            try:
                return probe(
                    self.adapter, chat_id=self.chat_id, chat_type=self.cfg.chat_type or None,
                    metadata=self.metadata,
                ) is True
            except TypeError:
                return bool(probe(self.adapter))
        except Exception:
            logger.debug("supports_card_streaming probe raised", exc_info=True)
            return False

    def _init_card_turn(self) -> None:
        from plugins.platforms.feishu.card_stream import CardStreamTurn
        title = "Hermes"
        try:
            title = self.adapter._card_title_text()  # Feishu adapter helper (bot display name)
        except Exception:
            title = str(getattr(self.adapter, "_bot_name", "") or "") or title
        self._card_turn = CardStreamTurn(title=title)
        self._card_last_send_ts = 0.0
        self._card_last_status_sig = ""
        self._card_last_payload_sig = ""
        self._card_pending_sep = False

    async def _prewarm_card(self) -> bool:
        """Send the card at reply start with an immediate status (no blank warm card)."""
        from plugins.platforms.feishu.card_stream import CARD_STREAM_METADATA_KEY
        turn = self._card_turn
        if turn is None:
            return False
        turn.mark_prewarmed()
        payload = turn.render_payload(time.monotonic(), final=False)
        meta = dict(self._metadata_for_send(final=False, expect_edits=True) or {})
        meta[CARD_STREAM_METADATA_KEY] = payload
        try:
            result = await self.adapter.send(
                chat_id=self.chat_id, content="", reply_to=self._initial_reply_to_id, metadata=meta,
            )
        except Exception as e:
            logger.debug("Streaming-card prewarm raised: %s", e)
            return False
        if not getattr(result, "success", False):
            logger.info("Streaming-card prewarm rejected: %s", getattr(result, "error", None))
            return False
        if not getattr(result, "message_id", None):
            return False
        self._adopt_message_id(result.message_id)
        self._track_preview_ids_from_result(result)
        self._card_last_send_ts = time.monotonic()
        self._card_last_status_sig = self._card_status_sig(payload)
        return True

    @staticmethod
    def _card_status_sig(payload: dict) -> str:
        from plugins.platforms.feishu.card_stream import status_signature
        return status_signature(payload)

    @staticmethod
    def _card_payload_sig(payload: dict) -> str:
        from plugins.platforms.feishu.card_stream import payload_signature
        return payload_signature(payload)

    @staticmethod
    def _card_split_body(full_body: str) -> tuple[str, str]:
        """Split an answer body into ``(visible_card_body, continuation_tail)``.

        Cards are bounded (``card_stream.fit_card_body``); anything past the card budget
        is delivered as continuation cards at turn-final so no text is lost.
        """
        from plugins.platforms.feishu.card_stream import CARD_BODY_MAX_CHARS, CARD_TRUNCATION_NOTICE
        body = str(full_body or "")
        if len(body) <= CARD_BODY_MAX_CHARS:
            return body, ""
        keep = max(0, CARD_BODY_MAX_CHARS - len(CARD_TRUNCATION_NOTICE))
        prefix = body[:keep].rstrip()
        return prefix + CARD_TRUNCATION_NOTICE, body[len(prefix):].lstrip()

    @staticmethod
    def _card_tail_chunks(text: str, limit: int = 6000) -> list[str]:
        """Split overflow text into card-sized chunks at line boundaries."""
        rest = str(text or "")
        chunks: list[str] = []
        while rest:
            if len(rest) <= limit:
                chunks.append(rest)
                break
            cut = rest.rfind("\n", 0, limit)
            if cut < limit // 3:
                cut = limit
            chunks.append(rest[:cut])
            rest = rest[cut:].lstrip("\n")
        return chunks

    async def _card_send_or_edit(self, *, finalize: bool = False, force: bool = False) -> bool:
        """Send or patch the streaming card for the current state."""
        from plugins.platforms.feishu.card_stream import CARD_STREAM_METADATA_KEY
        turn = self._card_turn
        if turn is None or not self._use_card_streaming:
            return False
        if self._accumulated:
            turn.set_body(self._accumulated)
        now = time.monotonic()
        payload = turn.render_payload(now, final=finalize)
        # Hide MEDIA:<path> / audio directives exactly like the stock stream transport
        # does: media is delivered separately after the turn, and the card body must
        # never show the raw directive.
        full_body = self._clean_for_display(str(payload.get("body") or ""))
        visible_body, continuation = self._card_split_body(full_body)
        if continuation:
            payload = {**payload, "body": visible_body, "truncated": True}
        if not finalize:
            payload_sig = self._card_payload_sig(payload)
            status_sig = self._card_status_sig(payload)
            changed = payload_sig != self._card_last_payload_sig or force
            status_changed = status_sig != self._card_last_status_sig
            if not changed:
                self._card_pending = False
                return True
            # Hard floor for every card PATCH: Feishu caps a message's patch rate at
            # ~5 QPS, so even a burst of tool events is coalesced to ≤4/s. The pending
            # flag makes the next tick retry the deferred state change.
            if (now - self._card_last_send_ts) < self._card_min_patch_interval:
                self._card_pending = True
                return True
            # Body-only growth is rate-limited to the streaming edit interval; tool /
            # narration status changes flush as soon as the hard floor allows.
            if not status_changed and not force and (now - self._card_last_send_ts) < self._card_update_interval:
                self._card_pending = True
                return True
        meta = dict(self._metadata_for_send(final=finalize, expect_edits=not finalize) or {})
        meta[CARD_STREAM_METADATA_KEY] = payload
        try:
            if self._message_id is None:
                result = await self.adapter.send(
                    chat_id=self.chat_id, content=visible_body,
                    reply_to=self._initial_reply_to_id, metadata=meta,
                )
            else:
                result = await self.adapter.edit_message(
                    chat_id=self.chat_id, message_id=self._message_id, content=visible_body,
                    finalize=finalize, metadata=meta,
                )
        except Exception as e:
            logger.warning("Streaming-card frame raised: %s", e)
            if finalize:
                # _card_finalize owns retries; do not delete the card between attempts.
                return False
            await self._card_discard_stale()
            self._card_downgrade("frame raised", keep_prefix=False)
            return False
        if not getattr(result, "success", False):
            error = str(getattr(result, "error", "") or "")
            self._last_card_error = error
            if _is_card_rate_limit(error):
                # Feishu's per-message update limit is transient: back off and retry on
                # the next tick instead of tearing the card down for the rest of the turn.
                self._card_min_patch_interval = min(3.0, max(1.0, self._card_min_patch_interval * 1.5))
                self._card_last_send_ts = time.monotonic()
                self._card_pending = True
                logger.warning(
                    "Streaming-card frame rate-limited; backing off to %.1fs intervals: %s",
                    self._card_min_patch_interval, error,
                )
                return False if finalize else True
            logger.warning("Streaming-card frame rejected: %s", error)
            if finalize:
                # Keep the card for the final retry/fold path; deleting it here would
                # force a duplicate card when the fallback re-renders the answer.
                return False
            await self._card_discard_stale()
            self._card_downgrade("frame rejected", keep_prefix=False)
            return False
        if self._message_id is None:
            message_id = getattr(result, "message_id", None)
            if not message_id:
                # The card may exist but can never be patched: stop here instead of
                # opening a second card on the next frame.
                logger.warning("Streaming-card frame delivered without a message id; downgrading")
                self._card_downgrade("missing message id")
                return False
            self._adopt_message_id(message_id)
            self._track_preview_ids_from_result(result)
        self._already_sent = True
        self._last_sent_text = visible_body
        self._card_last_send_ts = time.monotonic()
        self._card_last_payload_sig = self._card_payload_sig(payload)
        self._card_last_status_sig = self._card_status_sig(payload)
        self._card_pending = False
        if finalize:
            delivered = full_body
            if continuation:
                # Bounded head card + continuation card(s): the whole answer stays in
                # cards (no loose text bubble after the card).
                from plugins.platforms.feishu.card_stream import (
                    CARD_SCHEMA_VERSION, CARD_STREAM_METADATA_KEY as _CARD_KEY,
                    HEADER_TEMPLATE_FINAL as _FINAL_TEMPLATE,
                )
                tail_parts = self._card_tail_chunks(continuation)
                tail_ok = True
                for index, part in enumerate(tail_parts):
                    suffix = f" · 续 {index + 1}/{len(tail_parts)}" if len(tail_parts) > 1 else " · 续"
                    tail_payload = {
                        "schema": CARD_SCHEMA_VERSION,
                        "title": f"{turn.title}{suffix}",
                        "body": part,
                        "status": "",
                        "details": "",
                        "stats": {},
                        "chat_summary": "",
                        "footer": "",
                        "final": True,
                        "template": _FINAL_TEMPLATE,
                        "capsule": False,
                    }
                    tail_meta = dict(self._metadata_for_send(final=True, expect_edits=False) or {})
                    tail_meta[_CARD_KEY] = tail_payload
                    try:
                        tail_result = await self.adapter.send(
                            chat_id=self.chat_id, content=part, metadata=tail_meta,
                        )
                    except Exception as tail_err:
                        logger.warning("Streaming-card continuation card send raised: %s", tail_err)
                        tail_result = None
                    if not getattr(tail_result, "success", False):
                        logger.warning("Streaming-card continuation card send failed; gateway will resend the final")
                        tail_ok = False
                        break
                if not tail_ok:
                    delivered = visible_body
            self._final_response_sent = True
            self._mark_final_delivered(record=delivered)
        return True

    async def _card_discard_stale(self) -> None:
        """Recall a half-written card before the legacy text path delivers the answer.

        A stale card plus a separate full-text answer is the worst possible presentation
        (two versions of the same reply); dropping the card and resending the complete
        answer is cleaner. Rate-limited turns never reach here (they back off instead).
        """
        if self._message_id:
            with contextlib.suppress(Exception):
                await self.adapter.delete_message(self.chat_id, self._message_id)
            logger.info("Discarded stale streaming card %s", self._message_id)
        self._message_id = None
        self._already_sent = False
        self._last_sent_text = ""

    def _card_downgrade(self, reason: str, *, keep_prefix: bool = True) -> None:
        """Stop patching the card for this turn; the legacy transport finishes delivery.

        With ``keep_prefix`` the card stays on screen, so its visible prefix is inherited
        into the fallback and only the unseen tail is sent. When the card was discarded,
        the fallback must resend the whole answer.
        """
        logger.warning("Disabling streaming-card transport for this turn: %s", reason)
        self._use_card_streaming = False
        prefix = ""
        if keep_prefix:
            with contextlib.suppress(Exception):
                prefix = self._clean_for_display(self._last_sent_text or "")
        with contextlib.suppress(Exception):
            self._enter_fallback_mode(prefix)

    async def _card_push_update(self, tick: _Tick) -> None:
        """Render this tick's answer body/status into the card (final is _finalize_turn's job)."""
        if self._card_turn is None:
            return
        if tick.got_done:
            # The final frame is owned by _finalize_turn(): it can discard a warmed
            # empty card (silent/skip turns) instead of leaving a placeholder behind.
            return
        force = bool(getattr(tick, "card_dirty", False)) or bool(getattr(self, "_card_pending", False))
        await self._card_send_or_edit(finalize=False, force=force)

    async def _card_finalize(self) -> bool:
        """Close the card: final answer, status removed, footer added later by send_footer.

        Returns True when the card path handled the turn-final (including deleting an
        empty warmed card on a silent/skip turn); False means the caller should use the
        legacy final-delivery path.
        """
        turn = self._card_turn
        if turn is None or not self._use_card_streaming:
            return False
        await self._notify_before_finalize()
        if turn.discard_worthy():
            # Silent/skip turn with a warmed but empty card: discard it (reference
            # deletes the card instead of leaving a blank one in the chat).
            with contextlib.suppress(Exception):
                if self._message_id:
                    await self.adapter.delete_message(self.chat_id, self._message_id)
            self._message_id = None
            self._already_sent = False
            self._final_response_sent = False
            return True
        # The final frame is the one frame that must land. Retry transient 230020 with
        # exponential backoff (1.5/3/6s) and other failures once more; never delete the
        # card between attempts. If every attempt fails, leave the card in place: the
        # adapter's normal-final fold path either patches it (one card) or recalls it
        # before rendering a fresh final card (never two versions of the answer).
        delays = (0.0, 1.5, 3.0, 6.0)
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                logger.warning("Retrying final card frame in %.1fs (attempt %d/%d)", delay, attempt, len(delays))
                await asyncio.sleep(delay)
            self._last_card_error = ""
            if await self._card_send_or_edit(finalize=True, force=True):
                return True
            if attempt >= len(delays):
                break
        self._use_card_streaming = False
        self._final_response_sent = False
        from plugins.platforms.feishu.card_stream import record_card_event
        record_card_event("card_finalize_failed")
        logger.warning("Streaming-card finalize failed after retries; deferring to normal final fallback")
        return False

    async def _card_maybe_elapsed_refresh(self) -> None:
        """15s heartbeat: refresh the elapsed counter in whichever status line is showing.

        This used to fire only for an in-flight tool phase, so a long thinking/generation
        stretch left the card looking frozen (the gateway's separate "⏳ Working" bubble
        was the only movement). It now covers every non-final card state.
        """
        turn = self._card_turn
        if turn is None or self._message_id is None or not self._use_card_streaming:
            return
        if turn.final:
            return
        from plugins.platforms.feishu.card_stream import ELAPSED_REFRESH_SECONDS
        if (time.monotonic() - self._card_last_send_ts) >= ELAPSED_REFRESH_SECONDS:
            await self._card_send_or_edit(finalize=False, force=True)

    def _drain_queue(self) -> _Tick:
        """Drain everything queued so far into one tick.  Control sentinels stop the drain
        (they take effect this tick); _FINAL_TEXT / _TOOL_PROGRESS / text deltas fold into
        state so simultaneous items batch."""
        tick = _Tick()
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return tick
            if item is _DONE:
                tick.got_done = True
                return tick
            if item is _NEW_SEGMENT:
                tick.got_segment_break = True
                return tick
            if item is _REOPEN_SEED:
                tick.got_reopen_seed = True
                return tick
            kind = item[0] if isinstance(item, tuple) and item else None
            if kind is _FINAL_TEXT:
                self._adopt_final_text(item[1])
            elif kind is _TOOL_PROGRESS:  # keep draining to batch simultaneous lines
                if self._use_native_streaming or self._use_card_streaming:
                    self._tool_progress_lines.append(item[1])
                    self._tool_progress_active = True
            elif kind is _CARD_TOOL:  # streaming-card status: tool lifecycle event
                if self._card_turn is not None:
                    event_type, tool_name = item[1], item[2]
                    detail = item[3] if len(item) > 3 else ""
                    is_error = bool(item[4]) if len(item) > 4 else False
                    if event_type == "tool.started":
                        self._card_turn.on_tool_started(tool_name, detail=detail)
                    elif event_type == "tool.completed":
                        self._card_turn.on_tool_completed(is_error=is_error, detail=detail)
                    tick.card_dirty = True
            elif kind is _CARD_STEP:  # streaming-card status: agent iteration counter
                if self._card_turn is not None:
                    self._card_turn.on_step(item[1])
            elif kind is _CARD_COMMENT:  # streaming-card status: narration ticker
                if self._card_turn is not None:
                    self._card_turn.on_narration(item[1])
                    tick.card_dirty = True
            elif kind is _CARD_NOTICE:  # streaming-card status: transient gateway diagnostic
                if self._card_turn is not None:
                    self._card_turn.on_notice(item[1])
                    tick.card_dirty = True
            elif kind is _APPROVAL_BOUNDARY:
                tick.approval_boundary = (item[1], item[2])
                return tick
            elif kind is _COMMENTARY:
                tick.commentary_text = item[1]
                return tick
            elif kind is _FLUSH:
                # Barrier: finalize like a tool boundary, signal at the end of the tick.
                tick.got_flush = tick.got_segment_break = True
                tick.flush_event = item[1]
                return tick
            else:
                self._filter_and_accumulate(item)

    def _adopt_final_text(self, final_raw: str) -> None:
        """Adopt the authoritative final (see finish()) as the finalize content — only if this
        consumer streamed something (a no-stream turn keeps the gateway's final-send
        ownership).  Split delivery: wholesale adoption would repeat sealed heads, refusing
        makes the gateway resend the ENTIRE body — so append only the suffix when the final
        strictly prefix-extends the ledger."""
        if not (self._accumulated or self._message_id or self._last_sent_text):
            return
        if not self._turn_split_delivery:
            final_payload = self._clean_for_display(final_raw)
            if final_payload and final_payload != self._clean_for_display(self._accumulated):
                self._accumulated = final_raw
                self._stream_ledger = final_raw
            return
        ledger = self._stream_ledger
        if ledger and final_raw.startswith(ledger) and len(final_raw) > len(ledger):
            self._accumulated += final_raw[len(ledger):]
            self._stream_ledger = final_raw

    async def _eager_reopen_seed(self) -> None:
        """Eager re-seed after a clarify answer (gate re-checked: state may have advanced).
        Trade-off: WeCom's ~6-minute stream limit (errcode 846608, from the FIRST frame)
        now starts at the reply instant; on expiry we degrade to send()."""
        if not self._reopen_seed_pending():
            return
        if await self._try_seed_frame("Eager reopen seed raised, disabling native: %s"):
            self._native_stream_opened = True
            self._native_last_pushed_len = 0
            self._awaiting_reopen_after_boundary = False
            self._reopen_seeded_eagerly = True
            logger.info("[latency] Eager re-seed after clarify answer "
                        "(typing bubble reopened immediately, turn=%s)", self._turn_id)
        else:
            # Degrade to a single buffered send(), like the approval path.
            self._degrade_native_to_buffered_send()

    def _should_edit(self, tick: _Tick) -> bool:
        """Decide whether this tick flushes an edit/frame."""
        if not tick.is_interim:
            return True
        if self.cfg.buffer_only:
            return False
        if self._use_native_streaming:
            # No platform edit-rate limit: push every delta immediately.
            should_edit = bool(self._accumulated) or self._tool_progress_active
        else:
            elapsed = time.monotonic() - self._last_edit_time
            # buffer_threshold is a codepoint debounce heuristic, not a
            # platform-limit check (_len_fn is for overflow).  It must not
            # override an active flood backoff: while a refusal is being
            # waited out, only the (server-requested) interval may fire an edit.
            should_edit = bool((elapsed >= self._current_edit_interval and self._accumulated)
                               or (len(self._accumulated) >= self.cfg.buffer_threshold
                                   and not self._flood_strikes))
        # Defer mid-stream edits while the buffer could still resolve to a silence
        # marker ("NO"→"NO_REPLY"); got_done always resolves the buffer.
        return should_edit and not _is_partial_silence_marker(
            self._clean_for_display(self._accumulated))

    async def _split_first_send(self, tick: _Tick) -> bool:
        """No message to edit yet and the buffer overflows: seal only the head chunks; the
        tail stays in _accumulated as the active preview later deltas edit in place.
        True when the turn finished here (the run loop returns)."""
        chunks = self._truncate_for_stream(self._accumulated, self._safe_limit, self._len_fn)
        if len(chunks) <= 1:
            # Malformed/legacy adapter result must still be splittable.
            chunks = self._split_text_chunks(self._accumulated, self._safe_limit, self._len_fn)
        reply_to = self._initial_reply_to_id
        heads_delivered = len(chunks) > 1
        for chunk in chunks[:-1]:
            new_id = await self._send_new_chunk(chunk, reply_to, final=tick.got_done)
            if new_id is None or new_id == reply_to:
                heads_delivered = False  # keep the full text intact for the gateway fallback
                break
            reply_to = new_id

        if heads_delivered:
            # truncate_message suffixes multi-chunk output with " (n/n)"; the tail is the LIVE
            # preview later deltas extend, so a kept indicator ends up embedded mid-reply.
            tail = chunks[-1]
            indicator = f" ({len(chunks)}/{len(chunks)})"
            self._accumulated = tail.removesuffix(indicator)
            # Flag BEFORE the tail send: fresh-final replaces every tracked preview
            # with one message, which is only valid while the active message holds
            # the whole answer — deleting sealed heads drops delivered text.
            self._turn_split_delivery = True
        # Heads are sealed (or a later head failed): never edit a sealed message with
        # the unsplit payload — the tail is sent fresh, or the fallback path retries.
        self._message_id = None
        self._message_created_ts = None
        self._last_sent_text = ""
        self._last_edit_time = time.monotonic()
        if tick.got_done:
            tail_delivered = (not self._accumulated
                              or await self._send_or_edit(self._accumulated, finalize=True))
            # ``_already_sent`` may be True from prior state — only heads + tail count.
            self._final_response_sent = heads_delivered and tail_delivered
            if self._final_response_sent:
                self._turn_split_delivery = True
                self._mark_final_delivered(record=self._accumulated)
            return True
        if tick.got_segment_break:
            self._fallback_final_send = False
            self._fallback_prefix = ""
        return False

    def _overflows(self) -> bool:
        return self._len_fn(self._accumulated) > self._safe_limit

    def _first_send_overflows(self) -> bool:
        return self._message_id is None and self._overflows()

    async def _seal_overflow_heads(self) -> None:
        """Existing message overflowing: seal it with the head, start a new message for the rest."""
        while self._overflows() and self._message_id is not None and self._edit_supported:
            cp_budget = _custom_unit_to_cp(self._accumulated, self._safe_limit, self._len_fn)
            split_at = self._accumulated.rfind("\n", 0, cp_budget)
            if split_at < cp_budget // 2:
                split_at = cp_budget
            chunk = self._accumulated[:split_at]
            # finalize=True: the sealed chunk is never edited again, so it needs its
            # rich-text pass now.  is_turn_final=False: a split head is not the
            # answer, so fresh-final must not mark the turn delivered on it.
            ok = await self._send_or_edit(chunk, finalize=True, is_turn_final=False)
            if self._fallback_final_send or not ok:
                break  # keep the full text intact for the fallback final send
            self._accumulated = self._accumulated[split_at:].lstrip("\n")
            self._message_id = None
            self._last_sent_text = ""
            self._turn_split_delivery = True

    async def _push_update(self, tick: _Tick) -> None:
        """Send/edit this tick's visible text (cursor-suffixed unless finalizing)."""
        display_text = self._accumulated
        if tick.is_interim:
            if self._use_native_streaming:
                display_text = self._compose_frame_content()
                if display_text and self.cfg.cursor:
                    display_text += self.cfg.cursor
            else:
                display_text += self.cfg.cursor

        # A got_done FRESH send via the draft transport already carries finalize=True,
        # unlike an EDIT, which REQUIRES_EDIT_FINALIZE adapters still need a pass for.
        tick.draft_final_fresh_send = (tick.got_done and self._use_draft_streaming
                                       and self._message_id is None)
        # Segment break finalizes so platforms needing explicit closure (DingTalk AI
        # Cards) don't leave the segment stuck loading; it closes a preamble, not the
        # answer.
        tick.update_visible = await self._send_or_edit(
            display_text, finalize=tick.got_done or tick.got_segment_break,
            is_turn_final=tick.got_done)
        self._last_edit_time = time.monotonic()
        # Lines stay in _tool_progress_lines for the next compose.
        self._tool_progress_active = False

    async def _finalize_turn(self, tick: _Tick) -> None:
        """got_done: final edit without cursor, or one continuation send if edits failed."""
        if self._use_card_streaming:
            if await self._card_finalize():
                return
            # Card transport failed at the last frame → use the legacy final path.
            logger.warning("Streaming-card finalize failed; falling back to legacy delivery")
        if self._accumulated or self._message_id is not None or self._already_sent:
            await self._notify_before_finalize()
        if self._reopen_seed_pending() and not self._accumulated:
            # Lazy reopen, no post-prompt content: nothing is open on screen, so
            # don't re-seed just to emit a lone "✅".
            logger.debug("Clarify reopen boundary with no post-prompt content "
                         "— skipping lone-placeholder finalize (turn=%s)", self._turn_id)
        elif (self._reopen_seeded_eagerly and self._native_stream_opened
              and not self._accumulated and not tick.update_visible):
            # Eager seed, no content: the typing bubble IS on screen and would hang
            # forever — close it with an empty finalize.  Delivery flags untouched.
            await self._close_empty_native_bubble("Eager-seed empty finalize failed: %s")
            logger.debug("Eager reopen seed but no post-answer content — "
                         "closed empty typing bubble (turn=%s)", self._turn_id)
        elif self._use_native_streaming:
            # Native streams MUST close with finish=true even when empty (tool-only
            # turns) — placeholder if needed.
            if not tick.update_visible:
                await self._finalize_edit(self._accumulated or "✅", record=False)
            else:
                self._mark_final_delivered()
        elif self._accumulated:
            await self._finalize_edit_path(tick)

    async def _finalize_edit_path(self, tick: _Tick) -> None:
        """Edit-transport finalize (the non-native got_done branches, in priority order)."""
        if self._fallback_final_send:
            await self._send_fallback_final(self._accumulated)
        elif self._final_response_sent:
            # Fresh-final already delivered; a second finalize would duplicate.
            self._mark_final_delivered(record=self._accumulated)
        elif tick.update_visible and (not self._adapter_requires_finalize
                                      or self._last_edit_overflowed or tick.draft_final_fresh_send):
            # The update already delivered the final.  A second finalize would re-edit
            # it (Telegram: editMessageText after sendRichMessage falls back to the
            # legacy formatter) or overflow-split again, duplicating chunks.
            self._mark_skip_redundant_finalize()
        elif self._message_id:
            # No visible update this tick, or the adapter needs explicit finalize=True.
            # The edit may exhaust flood strikes → fallback mode: send the unsent tail.
            if not await self._finalize_edit(self._accumulated) and self._fallback_final_send:
                await self._send_fallback_final(self._accumulated)
        elif not self._already_sent:
            # Retry after the finalize tick failed.  finalize=True keeps stream-is-the-
            # message adapters out of the draft-frame branch, whose dedupe against the
            # last UNSEALED frame would report success with no transport call.
            await self._finalize_edit(self._accumulated)

    async def _finalize_edit(self, text: str, *, record: bool = True) -> bool:
        """finalize=True send_or_edit; on success mark the turn delivered (+ record payload)."""
        self._final_response_sent = await self._send_or_edit(text, finalize=True)
        if self._final_response_sent:
            self._mark_final_delivered(record=text if record else None)
        return self._final_response_sent

    def _cumulative_transport(self) -> bool:
        """Stream-is-the-message drafts, WeCom native and the Feishu streaming card:
        one append-only stream per turn."""
        stream_draft = self._stream_is_message() and self._use_draft_streaming
        return stream_draft or self._use_native_streaming or self._use_card_streaming

    async def _deliver_commentary(self, commentary_text: str) -> None:
        """Post commentary as its own message.  Cumulative transports keep the stream going —
        resetting _accumulated would break the append-only invariant / lose text.
        In card mode narration becomes the card's single ticker line (never a bubble)."""
        if self._use_card_streaming and self._card_turn is not None:
            self._card_turn.on_narration(commentary_text)
            # Not forced: when the drain already rendered this tick's narration the
            # payload signature is unchanged and no duplicate patch is sent.
            await self._card_send_or_edit(finalize=False, force=False)
            self._last_edit_time = time.monotonic()
            return
        cumulative = self._cumulative_transport()
        if not cumulative:
            self._reset_segment_state()
        await self._send_commentary(commentary_text)
        self._last_edit_time = time.monotonic()
        if not cumulative:
            self._reset_segment_state()

    async def _end_segment(self, tick: _Tick) -> None:
        """Tool boundary: edit-based transports reset so the next chunk is a fresh message.
        Cumulative transports must NOT reset — clearing _accumulated makes the next frame a
        non-prefix snapshot and the connector re-appends the whole answer.  preserve_no_edit:
        "__no_edit__" (platform never returned a real id — Signal, github_comment webhook)
        must keep its sentinel or every tool boundary posts a new message; the
        continuation goes out once via _send_fallback_final."""
        if self._use_card_streaming:
            # One card per turn: the next segment is appended under the current body
            # with a paragraph break instead of opening a second message.
            if self._accumulated:
                self._card_pending_sep = True
            return
        if self._cumulative_transport():
            return
        # If the segment-break edit or send didn't land (flood control / fallback mode, or a
        # failed first send of a split tail with no message id yet), _accumulated holds unseen
        # pre-boundary text — flush it before the reset clears it.
        if (self._accumulated and not tick.update_visible
                and self._message_id != "__no_edit__"):
            await self._flush_segment_tail_on_edit_failure()
        self._reset_segment_state(preserve_no_edit=True)

    async def _on_cancelled(self) -> None:
        """Best-effort final edit on task cancel: finalize=True so REQUIRES_EDIT_FINALIZE
        platforms apply formatting; is_turn_final=False because this handler owns the flags.
        Only a successful edit confirms delivery — a partial send may be just "Let me
        search…", not the answer."""
        if self._use_card_streaming and self._card_turn is not None:
            with contextlib.suppress(Exception):
                self._card_turn.finalize()
                if await self._card_send_or_edit(finalize=True, force=True):
                    return
        best_effort_ok = False
        if self._accumulated and self._message_id:
            with contextlib.suppress(Exception):
                best_effort_ok = bool(await self._send_or_edit(
                    self._accumulated, finalize=True, is_turn_final=False))
        elif self._message_id is None:
            # Draft path keeps _message_id=None; seal in place (else the stream stays
            # visibly live and the adapter keeps armed interception state).
            await self._abandon_native_stream()
        if best_effort_ok and not self._final_response_sent:
            self._mark_final_delivered(record=self._accumulated)

    def _wake_flush_waiters(self) -> None:
        """Wake still-queued _FLUSH waiters so a consumer dying mid-flush
        doesn't stall flush_pending_sync() for its full timeout."""
        with contextlib.suppress(Exception):
            while True:
                item = self._queue.get_nowait()
                if isinstance(item, tuple) and len(item) == 2 and item[0] is _FLUSH:
                    self._signal_flush(item[1])

    @staticmethod
    # Strip MEDIA:<path> tags before display. Uses the shared anchored MEDIA_TAG_CLEANUP_RE from
    # gateway/platforms/base.py — only tags whose path ends in a deliverable extension are removed, so an
    # unknown-extension path stays visible instead of being silently dropped (issue #34517). Streaming and
    # non-streaming paths share the same regex, so a tag is treated identically whichever path delivered the
    # text.
    def _clean_for_display(text: str) -> str:
        """Hide MEDIA:<path> / [[audio_as_voice]] directives; media is delivered post-stream."""
        return _BasePlatformAdapter.strip_media_directives_for_display(text)
