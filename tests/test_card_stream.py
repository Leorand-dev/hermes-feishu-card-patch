"""Unit tests for plugins/platforms/feishu/card_stream.py (final layout redesign).

Runs with the stdlib only (no pytest required):

    python tests/test_card_stream.py
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (
    os.path.join(_HERE, "..", "work", "plugins", "platforms", "feishu"),
    os.path.join(_HERE, "..", "plugins", "platforms", "feishu"),
    os.path.join(_HERE, "plugins", "platforms", "feishu"),
):
    if os.path.isdir(_cand):
        sys.path.insert(0, _cand)
        break

import card_stream as cs  # noqa: E402


def _new(**kwargs) -> cs.CardStreamTurn:
    turn = cs.CardStreamTurn(title="Hermes", now=1000.0, **kwargs)
    turn.mark_prewarmed(now=1000.0)
    return turn


def _elements(payload):
    return cs.build_card(payload)["elements"]


def _details_note(payload) -> str:
    for el in _elements(payload):
        if el["tag"] == "note":
            text = "".join(str(x.get("content") or "") for x in el["elements"])
            if "🧾" in text:
                return text
    return ""


def _stats_div(payload) -> dict:
    for el in _elements(payload):
        if el["tag"] == "div":
            return el
    return {}


def test_prewarm_layout_status_top() -> None:
    turn = _new()
    payload = turn.render_payload(now=1000.0)
    card = cs.build_card(payload)
    assert card["config"]["update_multi"] is True
    assert card["config"]["wide_screen_mode"] is True
    assert "summary" not in card["config"]
    assert card["header"]["title"]["content"] == "Hermes · 运行中"
    assert card["header"]["template"] == cs.HEADER_TEMPLATE_PROGRESS
    assert [el["tag"] for el in card["elements"]] == ["markdown", "markdown"]
    assert card["elements"][0]["content"] == cs.STATUS_PREWARM  # status at the TOP
    assert card["elements"][1]["content"] == cs.ZERO_WIDTH


def test_first_tool_status_and_details_line() -> None:
    turn = _new()
    turn.on_tool_started("read_file", now=1001.0)
    payload = turn.render_payload(now=1001.0)
    assert payload["status"].startswith("⏳ 已执行 1 个工具")
    assert "最后：read file" in payload["status"]
    details = _details_note(payload)
    assert details.startswith("🧾 运行详情")
    assert "共 1 次" in details
    assert "📖read file×1" in details


def test_tool_phase_elapsed_and_single_line() -> None:
    turn = _new()
    turn.on_tool_started("terminal", now=1000.0)
    turn.on_tool_completed(now=1001.0)
    turn.on_tool_started("terminal", now=1002.0)
    payload = turn.render_payload(now=1095.0)
    assert "已执行 2 个工具" in payload["status"]
    assert "最后：terminal" in payload["status"]
    assert " · 1m35s" in payload["status"]
    assert "\n" not in payload["status"]


def test_narration_is_single_primary_line() -> None:
    turn = _new()
    turn.on_narration("检查配置", now=1001.0)
    payload = turn.render_payload(now=1001.0)
    assert payload["status"] == "💬 检查配置"
    turn.on_tool_started("terminal", now=1002.0)
    payload = turn.render_payload(now=1002.0)
    assert "\n" not in payload["status"]
    assert payload["status"].startswith("⏳ 已执行 1 个工具")
    assert "检查配置" not in payload["status"]


def test_narration_cap_and_no_surrogate_split() -> None:
    turn = _new()
    turn.on_narration("长" * 200, now=1001.0)
    line = turn.narration
    assert len(line) <= cs.NARRATION_MAX_CHARS
    assert line.endswith("…")
    assert all(ord(ch) < 0xD800 or ord(ch) > 0xDFFF for ch in line)


def test_tool_done_switch() -> None:
    turn = _new()
    turn.on_tool_started("terminal", now=1000.0)
    turn.on_tool_completed(now=1001.0)
    turn.on_answer_started(now=1002.0)
    payload = turn.render_payload(now=1002.0)
    assert payload["status"] == "✅ 已执行 1 个工具 · 生成回复中…"


def test_processing_status_without_tools() -> None:
    turn = _new()
    turn.set_body("答案", now=1001.0)
    payload = turn.render_payload(now=1001.0)
    assert payload["status"] == cs.STATUS_PROCESSING


def test_final_layout_body_separator_summary_fields_footer() -> None:
    turn = _new()
    turn.on_tool_started("read_file", now=1000.0)
    turn.on_tool_completed(now=1001.0)
    turn.on_tool_started("terminal", now=1002.0)
    turn.on_tool_completed(now=1003.0)
    turn.on_tool_started("terminal", now=1004.0)
    turn.on_tool_completed(now=1005.0)
    turn.set_body("最终答案正文", now=1005.0)
    turn.finalize(footer="deepseek-flash · 42% · cache 87% · 110.8 tok/s · 3s", now=1006.0)
    payload = turn.render_payload(now=1006.0)
    assert payload["final"] is True
    assert payload["status"] == ""
    card = cs.build_card(payload)
    tags = [el["tag"] for el in card["elements"]]
    assert tags == ["markdown", "hr", "markdown", "div", "hr", "note"], tags
    assert card["elements"][0]["content"] == "最终答案正文"  # pure answer, no crammed quote
    summary = card["elements"][2]["content"]
    assert summary.startswith("> ✅ 完成")
    assert "3 个工具" in summary
    assert "3s" not in summary  # duration lives once in the footer latency
    div_text = json.dumps(card["elements"][3], ensure_ascii=False)
    assert "🧾 **运行详情**" in div_text
    assert "主要工具" in div_text and "💻 terminal×2" in div_text
    assert "⏱" not in div_text and "共 3 次" not in div_text  # no duplicated count/duration
    assert "cache 87%" in card["elements"][5]["elements"][0]["content"]
    assert "3s" in card["elements"][5]["elements"][0]["content"]
    assert card["config"]["summary"]["content"] == "最终答案正文"
    assert card["header"]["template"] == cs.HEADER_TEMPLATE_FINAL


def test_small_turn_final_has_no_details_block() -> None:
    turn = _new()
    turn.on_tool_started("read_file", now=1000.0)
    turn.on_tool_completed(now=1001.0)
    turn.on_tool_started("terminal", now=1002.0)
    turn.on_tool_completed(now=1003.0)
    turn.set_body("小回合答案", now=1003.0)
    turn.finalize(footer="model · 3s", now=1004.0)
    payload = turn.render_payload(now=1004.0)
    tags = [el["tag"] for el in _elements(payload)]
    assert tags == ["markdown", "hr", "markdown", "hr", "note"], tags
    assert _stats_div(payload) == {}


def test_final_error_layout_uses_error_fields() -> None:
    turn = _new()
    turn.on_tool_started("terminal", now=1000.0)
    turn.on_tool_completed(now=1001.0, is_error=True)
    turn.set_body("答案", now=1002.0)
    turn.finalize(now=1003.0)
    payload = turn.render_payload(now=1003.0)
    card = cs.build_card(payload)
    summary = next(el["content"] for el in card["elements"] if el.get("tag") == "markdown" and "完成" in el.get("content", ""))
    assert "⚠️ 完成但有错误" in summary and "1 个错误" in summary
    div_text = json.dumps(_stats_div(payload), ensure_ascii=False)
    assert "**⚠️ 错误**" in div_text and "terminal 执行失败" in div_text


def test_tool_only_final_has_no_empty_body_placeholder() -> None:
    turn = _new()
    turn.on_tool_started("terminal", now=1000.0)
    turn.on_tool_completed(now=1001.0)
    turn.on_tool_started("read_file", now=1002.0)
    turn.on_tool_completed(now=1003.0)
    turn.finalize(now=1003.0)
    payload = turn.render_payload(now=1003.0)
    tags = [el["tag"] for el in _elements(payload)]
    # Two tools are below DETAILS_MIN_TOOLS, so only the summary block remains.
    assert tags == ["markdown"], tags
    assert payload["body"] == ""
    assert _elements(payload)[0]["content"].startswith("> ✅ 完成 · 2 个工具")


def test_tool_only_final_with_many_tools_shows_details() -> None:
    turn = _new()
    for i in range(4):
        turn.on_tool_started("terminal", now=1000.0 + i)
        turn.on_tool_completed(now=1000.5 + i)
    turn.finalize(now=1010.0)
    payload = turn.render_payload(now=1010.0)
    tags = [el["tag"] for el in _elements(payload)]
    assert tags == ["markdown", "div"], tags
    div_text = json.dumps(_stats_div(payload), ensure_ascii=False)
    assert "💻 terminal×4" in div_text


def test_final_without_tools_has_only_body_and_footer() -> None:
    turn = _new()
    turn.set_body("纯文本答案", now=1001.0)
    turn.finalize(footer="deepseek-flash · 42%", now=1002.0)
    payload = turn.render_payload(now=1002.0)
    tags = [el["tag"] for el in _elements(payload)]
    assert tags == ["markdown", "hr", "note"], tags
    assert _elements(payload)[0]["content"] == "纯文本答案"


def test_summary_preview_fallback_for_empty_answer() -> None:
    turn = _new()
    turn.on_tool_started("terminal", now=1000.0)
    turn.on_tool_completed(now=1001.0)
    turn.finalize(now=1002.0)
    payload = turn.render_payload(now=1002.0)
    card = cs.build_card(payload)
    assert card["config"]["summary"]["content"] == "✅ 已回复"


def test_silent_turn_card_is_discard_worthy() -> None:
    turn = _new()
    assert turn.discard_worthy() is True
    turn.set_body("答案", now=1001.0)
    assert turn.discard_worthy() is False


def test_long_body_splits_into_elements() -> None:
    turn = _new()
    body = ("行" * 99 + "\n") * 60  # 6000 chars
    turn.set_body(body, now=1001.0)
    payload = turn.render_payload(now=1001.0)
    card = cs.build_card(payload)
    markdowns = [el for el in card["elements"] if el["tag"] == "markdown"]
    assert len(markdowns) >= 3
    joined = "".join(el["content"] for el in markdowns[1:])
    assert joined.replace("\n", "") == body.replace("\n", "")


def test_fit_card_body_truncates_and_points_to_continuation() -> None:
    short, truncated = cs.fit_card_body("普通答案")
    assert short == "普通答案" and truncated is False
    long_body = "汉" * (cs.CARD_BODY_MAX_CHARS + 500)
    visible, truncated = cs.fit_card_body(long_body)
    assert truncated is True
    assert len(visible) <= cs.CARD_BODY_MAX_CHARS
    assert cs.CARD_TRUNCATION_NOTICE in visible


def test_payload_signature_includes_details() -> None:
    turn = _new()
    p1 = turn.render_payload(now=1000.0)
    turn.on_tool_started("read_file", now=1001.0)
    p2 = turn.render_payload(now=1001.0)
    assert cs.payload_signature(p1) != cs.payload_signature(p2)
    assert cs.status_signature(p1) != cs.status_signature(p2)


def test_merge_cumulative_text() -> None:
    assert cs.merge_cumulative_text("", "abc") == "abc"
    assert cs.merge_cumulative_text("abc", "abc") == "abc"
    assert cs.merge_cumulative_text("abc", "abcde") == "abcde"
    assert cs.merge_cumulative_text("abcde", "def") == "abcdef"


def test_card_json_size_guard_with_details() -> None:
    turn = _new()
    turn.set_body("汉" * 9000, now=1001.0)
    turn.on_tool_started("terminal", now=1001.0, detail="x" * 100)
    payload = turn.render_payload(now=1001.0)
    card_json = cs.build_card_json(payload)
    assert not cs.card_json_too_large(card_json)
    parsed = json.loads(card_json)
    joined = "".join(el.get("content") or "" for el in parsed["elements"] if el.get("tag") == "markdown")
    assert cs.CARD_TRUNCATION_NOTICE in joined
    assert parsed["config"]["update_multi"] is True
    assert parsed["elements"][0]["tag"] == "markdown"  # status top
    assert parsed["elements"][-1]["tag"] == "note"  # details note (no footer set here)


def test_pipe_table_renders_as_column_set() -> None:
    turn = _new()
    turn.set_body(
        "结果如下：\n\n"
        "| 主机 | 状态 | 耗时 |\n"
        "|---|---|---|\n"
        "| 82 | 通过 | 1s |\n"
        "| 80 | 通过 | 2s |\n\n"
        "以上。",
        now=1001.0,
    )
    payload = turn.render_payload(now=1001.0)
    elements = _elements(payload)
    col_sets = [el for el in elements if el["tag"] == "column_set"]
    assert len(col_sets) == 1, [el["tag"] for el in elements]
    columns = col_sets[0]["columns"]
    assert len(columns) == 3
    text = json.dumps(columns, ensure_ascii=False)
    assert "82" in text and "通过" in text and "1s" in text
    markdown = "".join(el.get("content", "") for el in elements if el["tag"] == "markdown")
    assert "| 主机 |" not in markdown and "|---|" not in markdown


def test_wide_pipe_table_renders_as_list() -> None:
    turn = _new()
    turn.set_body(
        "| A | B | C | D | E |\n|---|---|---|---|---|\n| 1 | 2 | 3 | 4 | 5 |\n",
        now=1001.0,
    )
    payload = turn.render_payload(now=1001.0)
    elements = _elements(payload)
    assert not any(el["tag"] == "column_set" for el in elements)
    text = "\n".join(el.get("content", "") for el in elements if el["tag"] == "markdown")
    assert "**1**" in text and "4" in text


def test_table_inside_code_fence_is_not_converted() -> None:
    turn = _new()
    turn.set_body("```\n| A | B |\n|---|---|\n| 1 | 2 |\n```", now=1001.0)
    payload = turn.render_payload(now=1001.0)
    assert not any(el["tag"] == "column_set" for el in _elements(payload))


def test_notice_renders_as_status_and_clears_on_progress() -> None:
    turn = _new()
    turn.on_notice("⏳ Provider temporarily unavailable — retrying in 18s")
    payload = turn.render_payload(now=1001.0)
    assert payload["status"].startswith("⏳ Provider temporarily unavailable")
    turn.on_tool_started("terminal", now=1002.0)
    payload = turn.render_payload(now=1002.0)
    assert "Provider temporarily unavailable" not in payload["status"]


def test_final_error_body_uses_red_template() -> None:
    turn = _new()
    turn.set_body("⚠️ Provider unavailable after retries", now=1001.0)
    turn.finalize(now=1002.0)
    card = cs.build_card(turn.render_payload(now=1002.0))
    assert card["header"]["template"] == cs.HEADER_TEMPLATE_ERROR


def test_header_status_capsule_final_and_error() -> None:
    turn = _new()
    turn.set_body("答案", now=1001.0)
    turn.finalize(now=1002.0)
    card = cs.build_card(turn.render_payload(now=1002.0))
    assert card["header"]["title"]["content"] == "Hermes · 完成"
    error_turn = _new()
    error_turn.set_body("⚠️ failed", now=1001.0)
    error_turn.finalize(now=1002.0)
    card = cs.build_card(error_turn.render_payload(now=1002.0))
    assert card["header"]["title"]["content"] == "Hermes · 出错"
    assert card["header"]["template"] == cs.HEADER_TEMPLATE_ERROR


def test_tool_error_reason_reaches_details() -> None:
    turn = _new()
    turn.on_tool_started("terminal", now=1000.0)
    turn.on_tool_completed(now=1001.0, is_error=True, detail="command not found: foo")
    payload = turn.render_payload(now=1001.0)
    details = _details_note(payload)
    assert "terminal 执行失败：command not found: foo" in details


def test_card_metrics_record_events() -> None:
    cs.reset_card_metrics()
    cs.record_card_event("unit_test_event", ok=True)
    snap = cs.card_metric_snapshot()
    assert snap.get("unit_test_event") == 1


def test_cardkit_v2_streaming_and_final_shapes() -> None:
    import cardkit_v2 as ck

    turn = cs.CardStreamTurn(title="Hermes", now=1000.0)
    turn.mark_prewarmed(now=1000.0)
    for i in range(3):
        turn.on_tool_started("terminal", now=1001.0 + i)
        turn.on_tool_completed(now=1001.5 + i)
    streaming = turn.render_payload(now=1002.0)
    v2 = ck.build_card_v2(streaming, title_fallback="Hermes")
    assert v2["schema"] == "2.0"
    assert v2["config"]["streaming_mode"] is True
    assert v2["config"]["update_multi"] is True
    ids = [el.get("element_id") for el in v2["body"]["elements"]]
    assert "content" in ids and "status" in ids
    assert v2["header"]["text_tag_list"][0]["text"]["content"] == "生成中"

    turn.set_body(
        "答案\n\n| 主机 | 状态 |\n|---|---|\n| 82 | OK |\n",
        now=1003.0,
    )
    turn.finalize(footer="model · 42% · 3s", now=1004.0)
    final = turn.render_payload(now=1004.0)
    v2f = ck.build_card_v2(final, title_fallback="Hermes")
    assert v2f["config"]["streaming_mode"] is False
    tags = [el["tag"] for el in v2f["body"]["elements"]]
    assert "table" in tags and "collapsible_panel" in tags and "footer" in [
        el.get("element_id") for el in v2f["body"]["elements"]
    ]
    table = next(el for el in v2f["body"]["elements"] if el["tag"] == "table")
    assert table["columns"][0]["display_name"] == "主机"
    assert table["rows"][0]["c0"] == "82"
    assert v2f["header"]["text_tag_list"][0]["text"]["content"] == "完成"
    assert any(el.get("element_id") == "footer" for el in v2f["body"]["elements"])


def _run_all() -> int:
    failures = 0
    tests = [(name, obj) for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]
    for name, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failures}/{len(tests)} tests passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_all())
