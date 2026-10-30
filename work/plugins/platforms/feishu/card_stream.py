"""Feishu streaming-card presentation for Hermes Agent.

This module reproduces the *user-visible presentation* of the reference project
``Leorand-dev/openclaw-feishu-patch`` on Hermes' Feishu channel:

* one bounded interactive card per reply (never a wall of bubbles),
* the answer body streams inside the card,
* tool-call progress and mid-turn narration are folded into a compact
  dedicated status block (one primary line + at most one narration ticker line),
* the waiting line survives across tool batches and switches to
  ``✅ 已执行 N 个工具 · 生成回复中…`` once the answer starts,
* the final card removes the status block (no empty gap) and carries the
  runtime footer as a grey note,
* a prewarmed card is shown at reply start with an immediate status, and an
  empty/silent turn discards it instead of leaving a blank card behind.

Pure presentation/state logic only: no ``lark_oapi`` and no ``gateway`` imports,
so it can be unit-tested anywhere. The Feishu adapter turns :func:`build_card`
into ``msg_type="interactive"`` sends and ``im.v1.message.patch`` updates; the
gateway stream consumer drives :class:`CardStreamTurn`.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

CARD_STREAM_METADATA_KEY = "hermes_card_stream"
CARD_SCHEMA_VERSION = 1
DEFAULT_TITLE = "Hermes"

# --- lightweight observability ------------------------------------------------
# Counters + best-effort JSONL events. Never contains message content, credentials
# or chat ids: only event names, booleans and short error codes.
_METRICS_LOCK = threading.Lock()
_METRICS_COUNTS: dict[str, int] = {}
_CARD_METRICS_PATH = os.environ.get(
    "HERMES_FEISHU_CARD_METRICS",
    os.path.join(os.path.expanduser("~"), ".hermes", "logs", "feishu_card_metrics.jsonl"),
)


def record_card_event(event: str, **fields: Any) -> None:
    """Record one card-transport event (counts always; JSONL unless disabled)."""
    name = str(event or "unknown")
    with _METRICS_LOCK:
        _METRICS_COUNTS[name] = int(_METRICS_COUNTS.get(name, 0)) + 1
        if os.environ.get("HERMES_FEISHU_CARD_METRICS_DISABLE"):
            return
        try:
            os.makedirs(os.path.dirname(_CARD_METRICS_PATH), exist_ok=True)
            line = json.dumps({"ts": time.time(), "event": name, **fields}, ensure_ascii=False)
            with open(_CARD_METRICS_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass


def card_metric_snapshot() -> dict[str, int]:
    """In-process counters (useful for diagnostics/tests)."""
    with _METRICS_LOCK:
        return dict(_METRICS_COUNTS)


def reset_card_metrics() -> None:
    with _METRICS_LOCK:
        _METRICS_COUNTS.clear()

# Element/JSON budgets.  Feishu rejects oversized card JSON (the documented
# content cap for im/message patch/create is 30 KB); keep a safety margin.
CARD_ELEMENT_MAX_CHARS = 4000
CARD_MAX_BODY_ELEMENTS = 2            # 2 x 4000 = 8000 chars, Hermes' MAX_MESSAGE_LENGTH
CARD_BODY_MAX_CHARS = CARD_ELEMENT_MAX_CHARS * CARD_MAX_BODY_ELEMENTS
CARD_TRUNCATION_NOTICE = "\n\n…（内容较长，完整回复见后续卡片）"
CARD_JSON_MAX_BYTES = 28000
CARD_NOTE_MAX_CHARS = 240
CARD_DETAILS_MAX_CHARS = 240
CARD_SUMMARY_MAX_CHARS = 50

# Compact-status budgets (reference: 160 per line, 80 narration).
STATUS_LINE_MAX_CHARS = 160
STATUS_MAX_CHARS = 400
NARRATION_MAX_CHARS = 80
TOOL_LABEL_MAX_CHARS = 24
ELAPSED_REFRESH_SECONDS = 15.0
TOOL_ERRORS_MAX = 5

# Header templates.  Progress and final differ so the card reads as "working"
# and then "done"; the reference keeps a stable identity theme, but Hermes'
# earlier hand port used this pair and it stays recognizable.
HEADER_TEMPLATE_PROGRESS = "turquoise"
HEADER_TEMPLATE_FINAL = "blue"
HEADER_TEMPLATE_ERROR = "red"

ZERO_WIDTH = "\u200b"

STATUS_PREWARM = "⏳ 思考中…"
STATUS_PROCESSING = "✍️ 正在生成回复…"
STATUS_TOOLS_WAITING = "⏳ 已执行 {count} 个工具"
STATUS_TOOLS_DONE = "✅ 已执行 {count} 个工具 · 生成回复中…"
STATUS_LAST_TOOL = " · 最后：{label}"
STATUS_ELAPSED = " · {elapsed}"
STATUS_WAITING_SUFFIX = "，等待中…"
NARRATION_PREFIX = "💬 "

DETAILS_PANEL_TITLE = "🧾 运行详情"
DETAILS_MIN_TOOLS = 3  # small 1–2 tool turns don't need a details block
FINAL_SUMMARY_DONE = "✅ 完成"
FINAL_SUMMARY_ERRORS = "⚠️ 完成但有错误"
CHAT_SUMMARY_FALLBACK = "✅ 已回复"

# Tool label icons (mirrors the reference's per-tool emoji in the details panel).
TOOL_ICONS = {
    "terminal": "💻",
    "process": "⚙️",
    "background shell": "⚙️",
    "read file": "📖",
    "read": "📖",
    "write file": "✍️",
    "write": "✍️",
    "edit file": "🔧",
    "edit": "🔧",
    "apply patch": "🔧",
    "grep": "🔍",
    "glob": "📁",
    "ls": "📁",
    "web search": "🌐",
    "web fetch": "🌐",
    "browser": "🌐",
    "view image": "🖼️",
    "image": "🖼️",
    "memory": "🧠",
    "skill": "📝",
    "delegate": "🤝",
    "clarify": "❓",
}
DEFAULT_TOOL_ICON = "🔧"


_WS_RE = re.compile(r"\s+")
_MARKDOWN_DECOR_RE = re.compile(r"[*_`#>~]+")


def collapse_ws(text: str) -> str:
    """Collapse every whitespace run (incl. newlines) to one space and strip."""
    return _WS_RE.sub(" ", str(text or "")).strip()


def truncate_cp(text: str, limit: int) -> str:
    """Surrogate-safe truncation by code point, appending ``…`` when cut."""
    text = str(text or "")
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def format_elapsed(seconds: Optional[float]) -> str:
    """Humanize a status elapsed time: ``<1s``, ``12s``, ``1m05s``."""
    if seconds is None:
        return ""
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return ""
    if total < 1:
        return "<1s"
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    return f"{minutes}m{secs:02d}s"


def tool_label(tool_name: str) -> str:
    """Display label for a Hermes tool name (``read_file`` → ``read file``)."""
    label = collapse_ws(str(tool_name or "").replace("_", " "))
    return truncate_cp(label or "工具", TOOL_LABEL_MAX_CHARS)


def tool_icon(tool_name: str) -> str:
    """Emoji used for a tool in the ``🧾 运行详情`` panel."""
    label = tool_label(tool_name).lower()
    return TOOL_ICONS.get(label, DEFAULT_TOOL_ICON)


def summary_text(body: str, limit: int = CARD_SUMMARY_MAX_CHARS) -> str:
    """Chat-list preview: markdown/code stripped, ``✅ 已回复`` when there is no text."""
    clean = re.sub(r"```[\s\S]*?```", " ", str(body or ""))
    # Drop markdown table separator rows and pipe characters from the preview.
    clean = re.sub(r"(?m)^\s*\|?[\s:|-]+\|?\s*$", " ", clean)
    clean = clean.replace("|", " ")
    clean = collapse_ws(_MARKDOWN_DECOR_RE.sub(" ", clean))
    if not clean:
        return CHAT_SUMMARY_FALLBACK
    return truncate_cp(clean, limit)


def narration_line(text: str) -> str:
    """Latest-narration ticker line: one collapsed line capped at 80 chars."""
    body = collapse_ws(_MARKDOWN_DECOR_RE.sub(" ", str(text or "")))
    if not body:
        return ""
    body = truncate_cp(body, NARRATION_MAX_CHARS - len(NARRATION_PREFIX))
    return f"{NARRATION_PREFIX}{body}"


def split_body_elements(body: str, limit: int = CARD_ELEMENT_MAX_CHARS) -> list[str]:
    """Split the answer body into Feishu markdown elements at line boundaries."""
    body = str(body or "")
    if not body:
        return []
    if len(body) <= limit:
        return [body]
    chunks: list[str] = []
    rest = body
    while rest and len(chunks) < CARD_MAX_BODY_ELEMENTS:
        if len(rest) <= limit:
            chunks.append(rest)
            rest = ""
            break
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest and chunks:
        # Never silently drop answer text: fold the overflow into the last element
        # (the adapter refuses >CARD_JSON_MAX_BYTES and falls back to text instead).
        chunks[-1] = chunks[-1] + "\n" + rest
    return chunks


def fit_card_body(body: str) -> tuple[str, bool]:
    """Fit an answer body into the card's markdown budget.

    Cards are bounded: a very long answer would blow the 30 KB card JSON cap (and
    a >4000-char element is not something the reference sends either).  The card
    shows the head with a pointer, and the stream consumer sends the remainder as
    ordinary continuation messages once the turn finalizes, so no text is lost.
    Returns ``(visible_body, truncated)``.
    """
    body = str(body or "")
    if len(body) <= CARD_BODY_MAX_CHARS:
        return body, False
    keep = max(0, CARD_BODY_MAX_CHARS - len(CARD_TRUNCATION_NOTICE))
    return body[:keep].rstrip() + CARD_TRUNCATION_NOTICE, True


def _note_element(text: str) -> dict[str, Any]:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": truncate_cp(text, CARD_NOTE_MAX_CHARS)}]}


# --- markdown pipe-table conversion -----------------------------------------
# Classic Feishu cards do not render markdown tables: raw ``| ... |`` rows show up
# as body text. Detect compact pipe tables and render them as a column_set (≤4
# columns) or a readable list (wider tables).
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?\s*$")
CARD_TABLE_MAX_COLUMNS = 4
CARD_TABLE_MAX_ROWS = 20


def _split_pipe_row(line: str) -> list[str]:
    text = str(line or "").strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [collapse_ws(cell) for cell in text.split("|")]


def _table_block(lines: list[str], start: int) -> Optional[tuple[list[str], list[list[str]], int]]:
    """Return ``(headers, rows, next_index)`` when ``lines[start:]`` starts a table."""
    if start + 1 >= len(lines):
        return None
    header_line = lines[start]
    if header_line.count("|") < 2 or not _TABLE_SEP_RE.match(lines[start + 1]):
        return None
    headers = _split_pipe_row(header_line)
    if len(headers) < 2:
        return None
    rows: list[list[str]] = []
    i = start + 2
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.count("|") < 2:
            break
        cells = _split_pipe_row(line)
        if len(cells) < 2:
            break
        if len(cells) < len(headers):
            cells.extend([""] * (len(headers) - len(cells)))
        rows.append(cells[: len(headers)])
        i += 1
        if len(rows) >= CARD_TABLE_MAX_ROWS:
            break
    if not rows:
        return None
    return headers, rows, i


def _table_elements(headers: list[str], rows: list[list[str]]) -> list[dict[str, Any]]:
    """Render a table as column_set (narrow) or a header + bullet list (wide)."""
    columns = len(headers)
    if columns <= CARD_TABLE_MAX_COLUMNS:
        cols = []
        for index, header in enumerate(headers):
            values = [row[index] if index < len(row) else "" for row in rows]
            content = f"**{header}**\n" + "\n".join(v or "—" for v in values)
            cols.append({
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [{"tag": "markdown", "content": content}],
            })
        return [{
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "default",
            "columns": cols,
        }]
    intro = "**表格** · " + " · ".join(headers)
    lines = []
    for row in rows:
        cells = [cell for cell in row if cell]
        if cells:
            lines.append("- **" + cells[0] + "**" + (" · " + " · ".join(cells[1:]) if len(cells) > 1 else ""))
    return [
        {"tag": "markdown", "content": intro},
        {"tag": "markdown", "content": "\n".join(lines) if lines else "（空表）"},
    ]


def body_to_card_elements(body: str) -> list[dict[str, Any]]:
    """Split an answer body into markdown/card elements, converting pipe tables."""
    body = str(body or "")
    if not body:
        return []
    lines = body.splitlines()
    elements: list[dict[str, Any]] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        segment = "\n".join(buffer).strip("\n")
        buffer.clear()
        if not segment.strip():
            return
        elements.extend(
            {"tag": "markdown", "content": chunk}
            for chunk in split_body_elements(segment)
        )

    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            in_fence = not in_fence
            buffer.append(line)
            i += 1
            continue
        if not in_fence:
            table = _table_block(lines, i)
            if table is not None:
                flush()
                headers, rows, i = table
                elements.extend(_table_elements(headers, rows))
                continue
        buffer.append(line)
        i += 1
    flush()
    return elements



def details_note_element(content: str) -> dict[str, Any]:
    """Compact grey progress line used while the card is still streaming."""
    return {
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": truncate_cp(content, CARD_DETAILS_MAX_CHARS)}],
    }


def stats_div_element(stats: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Structured final ``🧾 运行详情`` block (only for meaningful turns).

    The completion summary already carries the tool count and the footer carries the
    elapsed time, so this block shows only the tool breakdown and errors: no duplicated
    counts/duration, no details for tiny 1–2 tool turns.
    """
    if not stats:
        return None
    total = int(stats.get("tool_total") or 0)
    errors = [str(e) for e in (stats.get("errors") or []) if str(e)]
    if total < DETAILS_MIN_TOOLS and not errors:
        return None
    fields: list[dict[str, Any]] = []
    counts = list(stats.get("counts") or [])
    if counts:
        parts = [f"{c.get('icon') or DEFAULT_TOOL_ICON} {c.get('label')}×{c.get('count')}" for c in counts[:8]]
        if len(counts) > 8:
            parts.append("其他×" + str(sum(int(c.get("count") or 0) for c in counts[8:])))
        fields.append({
            "is_short": False,
            "text": {"tag": "lark_md", "content": "**主要工具**\n" + " · ".join(parts)},
        })
    if errors:
        fields.append({
            "is_short": False,
            "text": {"tag": "lark_md", "content": "**⚠️ 错误**\n" + "；".join(errors[:2])},
        })
    if not fields:
        return None
    return {
        "tag": "div",
        "text": {"tag": "lark_md", "content": "🧾 **运行详情**"},
        "fields": fields,
    }


def build_card(payload: dict[str, Any], *, title_fallback: str = DEFAULT_TITLE) -> dict[str, Any]:
    """Build the Feishu card JSON for one streaming frame.

    Streaming frames: status line at the TOP, answer body, compact grey details line,
    footer note. Final frames: answer body, ``hr``, a blockquote completion summary,
    a structured ``🧾 运行详情`` div (tool/elapsed/error fields — roomy two-column
    layout instead of one crammed grey line), then ``hr`` + footer note.

    ``payload`` is the ``metadata["hermes_card_stream"]`` contract produced by
    :meth:`CardStreamTurn.render_payload`.
    """
    payload = payload or {}
    final = bool(payload.get("final"))
    body, _truncated = fit_card_body(str(payload.get("body") or ""))
    notice = "" if final else str(payload.get("notice") or "")
    status = notice or ("" if final else str(payload.get("status") or ""))
    footer = str(payload.get("footer") or "")
    details = truncate_cp(str(payload.get("details") or ""), CARD_DETAILS_MAX_CHARS)
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    chat_summary = str(payload.get("chat_summary") or "")
    summary = str((stats or {}).get("summary") or "")
    title = collapse_ws(str(payload.get("title") or "")) or str(title_fallback or DEFAULT_TITLE)
    errorish = final and (
        str(payload.get("template") or "") == HEADER_TEMPLATE_ERROR
        or str(payload.get("error") or "")
        or str(body).lstrip().startswith(("⚠️", "❌", "⛔"))
    )
    if payload.get("capsule", True):
        suffix = " · 出错" if errorish else " · 完成" if final else " · 运行中"
        if not title.endswith(suffix):
            title = f"{title}{suffix}"
    body_elements = body_to_card_elements(body)

    elements: list[dict[str, Any]] = []
    if final:
        if body_elements:
            elements.extend(body_elements)
        if summary:
            if body_elements:
                elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": f"> {summary}"})
        stats_block = stats_div_element(stats)
        if stats_block is not None:
            elements.append(stats_block)
        if not body_elements and not summary:
            elements.append({"tag": "markdown", "content": ZERO_WIDTH})
    else:
        if status:
            elements.append({"tag": "markdown", "content": status})
        if body_elements:
            elements.extend(body_elements)
        else:
            elements.append({"tag": "markdown", "content": ZERO_WIDTH})
        if details.strip():
            elements.append(details_note_element(details))
    if footer:
        elements.append({"tag": "hr"})
        elements.append(_note_element(footer))

    if errorish:
        template = HEADER_TEMPLATE_ERROR
    else:
        template = str(payload.get("template") or "").strip() or (
            HEADER_TEMPLATE_FINAL if final else HEADER_TEMPLATE_PROGRESS
        )
    config: dict[str, Any] = {"wide_screen_mode": True, "update_multi": True}
    if final:
        config["summary"] = {"content": chat_summary or summary_text(body)}
    return {
        "config": config,
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "elements": elements,
    }


def build_card_json(payload: dict[str, Any], *, title_fallback: str = DEFAULT_TITLE) -> str:
    """Serialize :func:`build_card` to the JSON string Feishu's API expects."""
    return json.dumps(build_card(payload, title_fallback=title_fallback), ensure_ascii=False)


def card_json_too_large(card_json: str) -> bool:
    return len(str(card_json or "").encode("utf-8")) > CARD_JSON_MAX_BYTES


def payload_signature(payload: dict[str, Any]) -> str:
    """Stable signature used to skip unchanged card patches."""
    if not payload:
        return ""
    return "\x1f".join(
        (
            "1" if payload.get("final") else "0",
            str(payload.get("template") or ""),
            str(payload.get("title") or ""),
            str(payload.get("body") or ""),
            str(payload.get("status") or ""),
            str(payload.get("details") or ""),
            str(payload.get("footer") or ""),
        )
    )


def status_signature(payload: dict[str, Any]) -> str:
    """Signature of the non-body parts (status/details/footer/final)."""
    if not payload:
        return ""
    return "\x1f".join(
        (
            "1" if payload.get("final") else "0",
            str(payload.get("status") or ""),
            str(payload.get("details") or ""),
            str(payload.get("footer") or ""),
        )
    )


def merge_cumulative_text(current: str, incoming: str) -> str:
    """Merge cumulative block text without duplicating already-shown prefixes.

    The gateway can deliver the same block more than once (retry / reorder); the
    reference ``mergeStreamingText`` drops equal copies, replaces a short-copy
    with its extension, merges prefix/suffix overlaps, and only then appends.
    """
    cur = str(current or "")
    new = str(incoming or "")
    if not new:
        return cur
    if not cur:
        return new
    if new == cur:
        return cur
    if cur.startswith(new):
        return cur
    if new.startswith(cur):
        return new
    # prefix/suffix overlap: "abcde" + "def" -> "abcdef"
    max_overlap = min(len(cur), len(new))
    for size in range(max_overlap, 0, -1):
        if cur.endswith(new[:size]):
            return cur + new[size:]
    return cur + new


@dataclass
class CardStreamTurn:
    """Per-turn state machine for one Feishu streaming card.

    One instance == one reply.  It is mutated from the gateway's stream-consumer
    loop (never concurrently) and rendered into ``hermes_card_stream`` payloads.

    Presentation follows the reference project's 2026.10.6 layout: a single
    phase status line at the TOP, the answer body, a compact streaming details
    line, and (at final) a separated completion summary + structured
    ``🧾 运行详情`` fields block + grey runtime-footer note.
    """

    title: str = DEFAULT_TITLE
    now: float = field(default_factory=time.monotonic)
    body: str = ""
    narration: str = ""
    footer: str = ""
    tool_total: int = 0
    tools_running: int = 0
    last_tool_label: str = ""
    last_tool_detail: str = ""
    tool_counts: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_errors: list[str] = field(default_factory=list)
    notice: str = ""
    turn_started_ts: Optional[float] = None
    tool_phase_started_ts: Optional[float] = None
    tool_phase_completed: bool = False
    answer_started: bool = False
    prewarmed: bool = False
    final: bool = False

    # ---- event mutations -------------------------------------------------

    def mark_prewarmed(self, now: Optional[float] = None) -> None:
        self.prewarmed = True
        if self.turn_started_ts is None:
            self.turn_started_ts = self.now if now is None else now

    def on_tool_started(self, name: str, now: Optional[float] = None, detail: str = "") -> None:
        now = self.now if now is None else now
        label = tool_label(name)
        self.notice = ""
        self.tool_total += 1
        self.tools_running += 1
        self.last_tool_label = label
        self.last_tool_detail = collapse_ws(str(detail or ""))[:60]
        entry = self.tool_counts.get(label)
        if entry is None:
            self.tool_counts[label] = {"icon": tool_icon(name), "label": label, "count": 1}
        else:
            entry["count"] = int(entry.get("count") or 0) + 1
        if self.tool_phase_started_ts is None:
            self.tool_phase_started_ts = now
        self.tool_phase_completed = False

    def on_tool_completed(self, now: Optional[float] = None, is_error: bool = False, detail: str = "") -> None:
        self.tools_running = max(0, self.tools_running - 1)
        if is_error:
            label = self.last_tool_label or "工具"
            icon = TOOL_ICONS.get(label.lower(), DEFAULT_TOOL_ICON)
            reason = collapse_ws(_MARKDOWN_DECOR_RE.sub(" ", str(detail or "")))
            if reason and reason.casefold() != label.casefold():
                self.tool_errors.append(f"{icon} {label} 执行失败：{truncate_cp(reason, 90)}")
            else:
                self.tool_errors.append(f"{icon} {label} 执行失败")
            del self.tool_errors[:-20]

    def on_step(self, iteration: int) -> None:
        """Accepted for API compatibility; the reference layout has no step counter."""
        return

    def on_answer_started(self, now: Optional[float] = None) -> None:
        if self.answer_started:
            return
        self.answer_started = True
        self.notice = ""
        if self.tool_total:
            self.tool_phase_completed = True

    def on_narration(self, text: str, now: Optional[float] = None) -> None:
        line = narration_line(text)
        if line:
            self.narration = line
            self.notice = ""

    def on_notice(self, text: str, now: Optional[float] = None) -> None:
        """Transient gateway diagnostic (provider retry, redirect) rendered in-card."""
        line = collapse_ws(_MARKDOWN_DECOR_RE.sub(" ", str(text or "")))
        if line:
            self.notice = truncate_cp(line, STATUS_LINE_MAX_CHARS)

    def set_body(self, text: str, now: Optional[float] = None) -> None:
        """Replace the body with the consumer's authoritative accumulated text."""
        self.body = str(text or "")
        if self.body:
            self.on_answer_started(now)

    def set_footer(self, footer: str) -> None:
        self.footer = truncate_cp(collapse_ws(footer), CARD_NOTE_MAX_CHARS)

    def finalize(self, footer: str = "", now: Optional[float] = None) -> None:
        if footer:
            self.set_footer(footer)
        self.final = True

    # ---- details line (compact grey note; classic cards cannot collapse) ---

    def _tools_compact(self) -> str:
        entries = sorted(self.tool_counts.values(), key=lambda e: int(e.get("count") or 0), reverse=True)
        if not entries:
            return ""
        parts = []
        for entry in entries[:5]:
            parts.append(f"{entry.get('icon') or DEFAULT_TOOL_ICON}{entry.get('label')}×{entry.get('count')}")
        if len(entries) > 5:
            rest = sum(int(e.get("count") or 0) for e in entries[5:])
            parts.append(f"其他×{rest}")
        return f"共 {self.tool_total} 次：" + "、".join(parts)

    def _errors_compact(self) -> str:
        if not self.tool_errors:
            return ""
        return f"⚠️ {len(self.tool_errors)} 个错误：" + "；".join(self.tool_errors[-2:])

    def details_text(self, now: Optional[float] = None) -> str:
        """Compact streaming progress line (tools / errors only; final uses fields)."""
        parts = []
        tools = self._tools_compact()
        if tools:
            parts.append(tools)
        errors = self._errors_compact()
        if errors:
            parts.append(errors)
        if not parts:
            return ""
        return truncate_cp(f"{DETAILS_PANEL_TITLE} · " + " · ".join(parts), CARD_DETAILS_MAX_CHARS)

    def stats(self, now: Optional[float] = None) -> dict[str, Any]:
        """Structured final statistics for the ``🧾 运行详情`` fields block."""
        entries = sorted(self.tool_counts.values(), key=lambda e: int(e.get("count") or 0), reverse=True)
        return {
            "tool_total": int(self.tool_total),
            "counts": [
                {"icon": e.get("icon") or DEFAULT_TOOL_ICON, "label": e.get("label"), "count": int(e.get("count") or 0)}
                for e in entries
            ],
            "errors": list(self.tool_errors[-TOOL_ERRORS_MAX:]),
            "summary": self.final_summary_line(now),
        }

    # ---- rendering -------------------------------------------------------

    def _waiting_primary(self, now: float) -> str:
        if self.tool_phase_completed:
            return STATUS_TOOLS_DONE.format(count=self.tool_total)
        primary = STATUS_TOOLS_WAITING.format(count=self.tool_total)
        details = []
        if self.last_tool_label:
            details.append(STATUS_LAST_TOOL.format(label=self.last_tool_label))
        if self.tool_phase_started_ts is not None:
            elapsed = format_elapsed(now - self.tool_phase_started_ts)
            if elapsed:
                details.append(STATUS_ELAPSED.format(elapsed=elapsed))
        if details:
            return primary + "".join(details)
        return primary + STATUS_WAITING_SUFFIX

    def status_text(self, now: Optional[float] = None) -> str:
        """Single phase status line (reference: no second narration line)."""
        if self.final:
            return ""
        if self.notice:
            return truncate_cp(self.notice, STATUS_LINE_MAX_CHARS)
        now = self.now if now is None else now
        if self.tool_total:
            primary = self._waiting_primary(now)
        elif self.narration:
            primary = self.narration
        elif self.answer_started or self.body:
            primary = STATUS_PROCESSING
        else:
            primary = STATUS_PREWARM
        return truncate_cp(primary, STATUS_LINE_MAX_CHARS)

    def final_summary_line(self, now: Optional[float] = None) -> str:
        # Duration is shown once, in the footer's latency field; the summary carries
        # only the outcome and tool/error counts to avoid 2–3 near-identical timings.
        if not (self.tool_total or self.tool_errors):
            return ""
        parts = [FINAL_SUMMARY_ERRORS if self.tool_errors else FINAL_SUMMARY_DONE]
        if self.tool_total:
            parts.append(f"{self.tool_total} 个工具")
        if self.tool_errors:
            parts.append(f"{len(self.tool_errors)} 个错误")
        return " · ".join(parts)

    def render_payload(self, now: Optional[float] = None, *, final: Optional[bool] = None) -> dict[str, Any]:
        """Build the ``hermes_card_stream`` payload for the current state."""
        now = self.now if now is None else now
        is_final = self.final if final is None else bool(final)
        if self.turn_started_ts is None:
            self.turn_started_ts = now
        if is_final:
            self.on_answer_started(now)
            status = ""
            body = self.body
            details = ""
            stats = self.stats(now)
            chat_summary = summary_text(self.body) if self.body.strip() else CHAT_SUMMARY_FALLBACK
        else:
            status = self.status_text(now)
            body = self.body
            details = self.details_text(now)
            stats = self.stats(now)
            chat_summary = ""
        payload = {
            "schema": CARD_SCHEMA_VERSION,
            "title": self.title,
            "body": body,
            "status": status,
            "notice": "" if is_final else self.notice,
            "details": details,
            "stats": stats,
            "chat_summary": chat_summary,
            "footer": self.footer,
            "final": is_final,
            "template": HEADER_TEMPLATE_FINAL if is_final else HEADER_TEMPLATE_PROGRESS,
        }
        return payload

    def discard_worthy(self) -> bool:
        """An empty prewarmed card (silent/skip turn) should be deleted, not left blank."""
        return bool(
            self.prewarmed
            and not self.final
            and not self.body
            and not self.tool_total
            and not self.narration
            and not self.notice
        )
