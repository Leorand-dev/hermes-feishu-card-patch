"""CardKit 2.0 (schema 2.0) builders for the Feishu streaming card.

Optional transport: classic card JSON remains the default because older Feishu
clients render a "please upgrade" block for 2.0 cards. When the client supports
2.0 (>= 7.20), the adapter can use card entities + element-level content updates
and a final full-card swap with native table / collapsible_panel elements.

This module is pure (no lark_oapi imports) so it can be unit-tested.
"""

from __future__ import annotations

import json
from typing import Any, Optional

try:  # Hermes package context
    from plugins.platforms.feishu import card_stream as _cs
except ImportError:  # unit tests add the feishu dir to sys.path
    import card_stream as _cs  # type: ignore[no-redef]

SCHEMA = "2.0"


def _element_id(prefix: str, index: int) -> str:
    # CardKit: [A-Za-z][A-Za-z0-9_]{0,19}
    return f"{prefix}{index}"[:20]


def _markdown(element_id: str, content: str) -> dict[str, Any]:
    return {"tag": "markdown", "element_id": element_id, "content": content or "\u200b"}


def _header(payload: dict[str, Any], title_fallback: str, final: bool, errorish: bool) -> dict[str, Any]:
    base = _cs.collapse_ws(str(payload.get("title") or "")) or str(title_fallback or _cs.DEFAULT_TITLE)
    if final:
        label = "出错" if errorish else "完成"
        color = "red" if errorish else "blue"
        subtitle = "错误" if errorish else "完成"
    else:
        label = "生成中"
        color = "wathet"
        subtitle = "正在生成…"
    return {
        "title": {"tag": "plain_text", "content": base},
        "subtitle": {"tag": "plain_text", "content": subtitle},
        "text_tag_list": [
            {"tag": "text_tag", "text": {"tag": "plain_text", "content": label}, "color": color},
        ],
        "template": "red" if errorish else ("blue" if final else "wathet"),
    }


def _table_elements(body: str, *, limit_tables: int = 5) -> list[dict[str, Any]]:
    """Convert markdown pipe tables to native 2.0 table elements; returns [] if none."""
    lines = str(body or "").splitlines()
    tables: list[dict[str, Any]] = []
    i = 0
    while i < len(lines) and len(tables) < limit_tables:
        parsed = None
        try:
            parsed = _cs._table_block(lines, i)  # type: ignore[attr-defined]
        except Exception:
            parsed = None
        if parsed is None:
            i += 1
            continue
        headers, rows, i = parsed
        columns = [
            {"name": f"c{idx}", "display_name": str(header or f"列{idx + 1}"),
             "data_type": "text", "width": "auto"}
            for idx, header in enumerate(headers)
        ]
        table_rows = []
        for row in rows:
            table_rows.append({f"c{idx}": str(row[idx] if idx < len(row) else "") for idx in range(len(headers))})
        tables.append({
            "tag": "table",
            "element_id": _element_id("tbl", len(tables)),
            "page_size": 10,
            "row_height": "auto",
            "freeze_first_column": True,
            "header_style": {"text_align": "left", "text_size": "normal",
                             "background_style": "none", "text_color": "grey",
                             "bold": True, "lines": 1},
            "columns": columns,
            "rows": table_rows,
        })
    return tables


def _body_markdown_without_tables(body: str) -> str:
    """Remove table blocks from streaming body (tables appear only in the final swap)."""
    lines = str(body or "").splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        parsed = None
        try:
            parsed = _cs._table_block(lines, i)  # type: ignore[attr-defined]
        except Exception:
            parsed = None
        if parsed is not None:
            _headers, _rows, i = parsed
            out.append("（表格将在完成后渲染）")
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out).strip()


def _details_panel(stats: dict[str, Any]) -> Optional[dict[str, Any]]:
    total = int((stats or {}).get("tool_total") or 0)
    errors = [str(e) for e in ((stats or {}).get("errors") or []) if str(e)]
    if total < 3 and not errors:
        return None
    lines: list[str] = []
    counts = list((stats or {}).get("counts") or [])
    if counts:
        parts = [f"{c.get('icon') or _cs.DEFAULT_TOOL_ICON} {c.get('label')}×{c.get('count')}" for c in counts[:8]]
        if len(counts) > 8:
            parts.append("其他×" + str(sum(int(c.get("count") or 0) for c in counts[8:])))
        lines.append("**主要工具**\n" + " · ".join(parts))
    if errors:
        lines.append("**⚠️ 错误**\n" + "；".join(errors[:2]))
    return {
        "tag": "collapsible_panel",
        "element_id": "detailspanel",
        "expanded": False,
        "header": {
            "title": {"tag": "plain_text", "content": "🧾 运行详情"},
            "vertical_align": "center",
            "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined",
                     "color": "", "size": "16px 16px"},
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "border": {"color": "grey", "corner_radius": "5px"},
        "vertical_spacing": "8px",
        "padding": "8px 8px 8px 8px",
        "elements": [_markdown("detailspanel_body", "\n\n".join(lines) or "\u200b")],
    }


def _footer_element(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    footer = str(payload.get("footer") or "").strip()
    if not footer:
        return None
    return _markdown("footer", f"<font color='grey'>{footer}</font>")


def build_card_v2(payload: dict[str, Any], *, title_fallback: str = _cs.DEFAULT_TITLE) -> dict[str, Any]:
    """Build a schema-2.0 card for a streaming or final frame."""
    payload = payload or {}
    final = bool(payload.get("final"))
    body, _truncated = _cs.fit_card_body(str(payload.get("body") or ""))
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    summary = str((stats or {}).get("summary") or "")
    chat_summary = str(payload.get("chat_summary") or "")
    errorish = final and (
        str(payload.get("template") or "") == _cs.HEADER_TEMPLATE_ERROR
        or str(body).lstrip().startswith(("⚠️", "❌", "⛔"))
    )
    elements: list[dict[str, Any]] = []
    if final:
        # Native table elements are root-level; text around them becomes markdown.
        lines, text_parts = str(body or "").splitlines(), []
        i = 0
        buf: list[str] = []
        while i < len(lines):
            parsed = None
            try:
                parsed = _cs._table_block(lines, i)  # type: ignore[attr-defined]
            except Exception:
                parsed = None
            if parsed is not None:
                _headers, _rows, i = parsed
                if buf:
                    text_parts.append("\n".join(buf).strip())
                    buf = []
                continue
            buf.append(lines[i])
            i += 1
        if buf:
            text_parts.append("\n".join(buf).strip())
        # Re-run table conversion for the whole body (compact; max 5 tables).
        table_elements = _table_elements(body)
        if text_parts:
            for index, part in enumerate(p for p in text_parts if p):
                elements.append(_markdown(_element_id("content", index), part))
        elif not table_elements:
            elements.append(_markdown("content", _cs.ZERO_WIDTH))
        if summary:
            elements.append(_markdown("summary", f"> {summary}"))
        elements.extend(table_elements)
        panel = _details_panel(stats)
        if panel is not None:
            elements.append(panel)
    else:
        status = str(payload.get("status") or payload.get("notice") or "")
        if status:
            elements.append(_markdown("status", status))
        streaming_body = _body_markdown_without_tables(body)
        elements.append(_markdown("content", streaming_body or _cs.ZERO_WIDTH))
    footer = _footer_element(payload)
    if final and footer is not None:
        elements.append(footer)
    config: dict[str, Any] = {
        "streaming_mode": not final,
        "update_multi": True,
        "summary": {"content": chat_summary or _cs.summary_text(body)},
        "streaming_config": {
            "print_frequency_ms": {"default": 50},
            "print_step": {"default": 1},
            "print_strategy": "fast",
        },
    }
    return {
        "schema": SCHEMA,
        "config": config,
        "header": _header(payload, title_fallback, final, errorish),
        "body": {"direction": "vertical", "elements": elements},
    }


def build_card_v2_json(payload: dict[str, Any], *, title_fallback: str = _cs.DEFAULT_TITLE) -> str:
    return json.dumps(build_card_v2(payload, title_fallback=title_fallback), ensure_ascii=False)
