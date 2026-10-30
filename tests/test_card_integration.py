"""Integration tests for the Feishu streaming-card patch (run on the Hermes host).

Exercises the REAL ``FeishuAdapter`` + ``GatewayStreamConsumer`` against an
in-memory fake lark client (no network, no credentials):

    cd <patched-hermes-src> && PYTHONPATH=. <hermes-python> tests/test_card_integration.py

Covers:
  * one interactive card per turn (send once, patch many times, never a second bubble),
  * tool progress + narration folded into the card status (no progress bubbles),
  * final card = answer + hr + footer note, status element gone,
  * 15s elapsed heartbeat patch while a tool batch runs,
  * non-streamed final response rendered as a finished card,
  * silent/empty turn discards the prewarmed card.
"""

from __future__ import annotations

import asyncio
import json
import sys
import traceback

try:  # import the modules under test (repo root on PYTHONPATH)
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
    from plugins.platforms.feishu.adapter import FeishuAdapter
    from plugins.platforms.feishu import card_stream as cs
except Exception as exc:  # pragma: no cover - environment guard
    print(f"IMPORT FAILED: {type(exc).__name__}: {exc}")
    traceback.print_exc()
    sys.exit(2)


class FakeResponse:
    def __init__(self, message_id: str = "om_card_1", code: int = 0, msg: str = "success"):
        self.code = code
        self.msg = msg
        self.data = type("Data", (), {"message_id": message_id})()

    def success(self) -> bool:
        return self.code == 0


class FakeMessageAPI:
    """Records every Feishu im/message call the adapter makes."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._seq = 0
        # Failure injection for audit/robustness tests.
        self.fail_interactive_sends = False
        self.patch_raises = False
        self.update_fails = False
        self.patch_rate_limit_attempts = 0

    def _record(self, op: str, request, *, ok: bool = True) -> None:
        self._seq += 1
        body = getattr(request, "request_body", None)
        content = getattr(body, "content", "") if body is not None else ""
        msg_type = getattr(body, "msg_type", "") if body is not None else ""
        self.calls.append({
            "op": op,
            "message_id": getattr(request, "message_id", ""),
            "content": content,
            "msg_type": msg_type,
            "ok": ok,
        })

    def _resp(self) -> FakeResponse:
        return FakeResponse(f"om_card_{self._seq}")

    def _interactive_failure(self, request):
        body = getattr(request, "request_body", None)
        if self.fail_interactive_sends and getattr(body, "msg_type", "") == "interactive":
            return FakeResponse("", code=99991400, msg="interactive send rejected")
        return None

    def reply(self, request) -> FakeResponse:
        failure = self._interactive_failure(request)
        self._record("reply", request, ok=failure is None)
        return failure or self._resp()

    def create(self, request) -> FakeResponse:
        failure = self._interactive_failure(request)
        self._record("create", request, ok=failure is None)
        return failure or self._resp()

    def patch(self, request) -> FakeResponse:
        if self.patch_raises:
            self._record("patch", request, ok=False)
            raise RuntimeError("patch boom")
        if self.patch_rate_limit_attempts > 0:
            self.patch_rate_limit_attempts -= 1
            self._record("patch", request, ok=False)
            return FakeResponse("", code=230020,
                                msg="This operation triggers the frequency limit")
        self._record("patch", request)
        return self._resp()

    def update(self, request) -> FakeResponse:
        self._record("update", request, ok=not self.update_fails)
        if self.update_fails:
            return FakeResponse("", code=230001, msg="invalid msg_type")
        return self._resp()

    def delete(self, request) -> FakeResponse:
        self._record("delete", request)
        return FakeResponse("", code=0)

    # helpers for assertions
    def ok_sends(self) -> list[dict]:
        return [c for c in self.sends() if c["ok"]]

    def sends(self) -> list[dict]:
        return [c for c in self.calls if c["op"] in ("reply", "create") and c["msg_type"] == "interactive"]

    def text_sends(self) -> list[dict]:
        return [c for c in self.calls if c["op"] in ("reply", "create") and c["msg_type"] in ("text", "post")]

    def patches(self) -> list[dict]:
        return [c for c in self.calls if c["op"] == "patch"]

    def deletes(self) -> list[dict]:
        return [c for c in self.calls if c["op"] == "delete"]


def make_adapter(card_replies: bool = True) -> tuple[FeishuAdapter, FakeMessageAPI]:
    api = FakeMessageAPI()
    adapter = FeishuAdapter.__new__(FeishuAdapter)  # bypass connect/__init__ side effects
    message_api = type("Message", (), {
        "reply": api.reply, "create": api.create, "patch": api.patch,
        "update": api.update, "delete": api.delete,
    })()
    adapter._client = type("Client", (), {
        "im": type("Im", (), {"v1": type("V1", (), {"message": message_api})()})(),
    })()
    adapter._card_replies = card_replies
    adapter._bot_name = "Hermes"
    adapter._live_stream_cards = {}
    adapter._domain_name = "feishu"
    adapter._admins = set()
    adapter._allowed_group_users = set()
    adapter._sender_name_cache = {}
    return adapter, api


async def test_non_streamed_final_over_card_budget_falls_back_to_text() -> None:
    adapter, api = make_adapter()
    body = "汉" * (cs.CARD_BODY_MAX_CHARS + 500)
    result = await adapter.send("oc_test", body, metadata={"is_final_response": True})
    assert result.success, result.error
    assert api.sends() == [], fills(api.calls)  # no silently truncated card
    texts = api.text_sends()
    assert texts, fills(api.calls)
    assert cs.CARD_TRUNCATION_NOTICE not in "".join(c["content"] for c in texts)


def card_json(call: dict) -> dict:
    return json.loads(call["content"])


async def wait_until(pred, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met before timeout")


# ---------------------------------------------------------------------------
# adapter-level
# ---------------------------------------------------------------------------

async def test_non_streamed_final_renders_card() -> None:
    adapter, api = make_adapter()
    result = await adapter.send(
        "oc_test", "最终答案正文", metadata={
            "is_final_response": True,
            "runtime_footer": "deepseek-flash · 42% · cache 87% · 110.8 tok/s",
        },
    )
    assert result.success, result.error
    sends = api.sends()
    assert len(sends) == 1, api.calls
    card = card_json(sends[0])
    tags = [el["tag"] for el in card["elements"]]
    assert tags == ["markdown", "hr", "note"], tags
    assert card["elements"][0]["content"] == "最终答案正文"
    assert "cache 87%" in card["elements"][2]["elements"][0]["content"]
    assert card["config"]["update_multi"] is True
    assert card["config"]["summary"]["content"] == "最终答案正文"


async def test_footer_folds_into_live_card() -> None:
    adapter, api = make_adapter()
    payload = {
        "schema": 1, "title": "Hermes", "body": "答案", "status": "✅ 已执行 1 个工具 · 生成回复中…",
        "details": "**🔧 工具调用**\n\n共 1 次\n- 💻 terminal x1",
        "footer": "", "final": False, "template": "turquoise",
    }
    res = await adapter.send("oc_test", "答案", metadata={"hermes_card_stream": payload})
    assert res.success and api.sends()
    res2 = await adapter.send_footer("oc_test", "deepseek-flash · 42% · cache 87% · 110.8 tok/s")
    assert res2.success
    patches = api.patches()
    assert patches, api.calls
    card = card_json(patches[-1])
    tags = [el["tag"] for el in card["elements"]]
    # Footer fold marks the card final: body + separator + footer note. This payload
    # has no tool stats, so there is no completion summary/details block.
    assert tags == ["markdown", "hr", "note"], tags
    assert "cache 87%" in card["elements"][2]["elements"][0]["content"]
    # no second message was created for the footer
    assert len(api.sends()) == 1


# ---------------------------------------------------------------------------
# stream consumer end-to-end (one card per turn)
# ---------------------------------------------------------------------------

async def _run_turn(adapter: FeishuAdapter, script, *, min_patch_interval: float = 0.01) -> GatewayStreamConsumer:
    consumer = GatewayStreamConsumer(
        adapter=adapter, chat_id="oc_test",
        config=StreamConsumerConfig(edit_interval=0.01), metadata={},
        initial_reply_to_id="om_user",
    )
    # Tests exercise state transitions, not the production 0.25s patch floor.
    consumer._card_min_patch_interval = min_patch_interval
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.1)  # let transports resolve + the card prewarm land
    await script(consumer)
    await asyncio.wait_for(task, 10)
    return consumer


async def test_one_card_per_turn_with_status_and_narration() -> None:
    adapter, api = make_adapter()

    async def script(consumer: GatewayStreamConsumer) -> None:
        consumer.on_card_tool("tool.started", "terminal")
        consumer.on_card_commentary("检查日志")
        await asyncio.sleep(0.05)
        consumer.on_delta("答案正文")
        await asyncio.sleep(0.05)
        consumer.on_card_tool("tool.completed", "terminal")
        consumer.finish("答案正文")

    consumer = await _run_turn(adapter, script)
    assert consumer.final_response_sent is True

    sends = api.sends()
    assert len(sends) == 1, fills(api.calls)  # exactly one interactive card created
    patches = api.patches()
    assert len(patches) >= 2, fills(api.calls)

    # Mid-turn frame: status block carries the tool progress (and narration ticker).
    mid_status = [
        el["content"] for call in patches[:-1] for el in card_json(call)["elements"]
        if el.get("tag") == "markdown" and "工具" in str(el.get("content", ""))
    ]
    assert mid_status, fills(api.calls)

    # Final frame: status removed; answer body, separator, completion summary.
    # A single tool is below DETAILS_MIN_TOOLS, so there is no details block.
    final_card = card_json(patches[-1])
    tags = [el["tag"] for el in final_card["elements"]]
    assert tags == ["markdown", "hr", "markdown"], tags
    body = final_card["elements"][0]["content"]
    assert body.count("答案正文") == 1
    assert final_card["elements"][2]["content"].startswith("> ✅ 完成 · 1 个工具")
    assert final_card["header"]["template"] == cs.HEADER_TEMPLATE_FINAL
    assert api.deletes() == []


def fills(calls) -> str:
    return "calls=" + json.dumps(calls, ensure_ascii=False)[:3000]


async def test_tool_only_turn_final_body() -> None:
    adapter, api = make_adapter()

    async def script(consumer: GatewayStreamConsumer) -> None:
        consumer.on_card_tool("tool.started", "terminal")
        consumer.on_card_tool("tool.completed", "terminal")
        consumer.on_card_tool("tool.started", "read_file")
        consumer.on_card_tool("tool.completed", "read_file")
        await asyncio.sleep(0.05)
        consumer.finish("")

    await _run_turn(adapter, script)
    patches = api.patches()
    assert patches, fills(api.calls)
    final_card = card_json(patches[-1])
    assert final_card["elements"][0]["content"].startswith("> ✅ 完成 · 2 个工具"), final_card
    # Two tools are below DETAILS_MIN_TOOLS: no details block for tiny turns.
    assert [el["tag"] for el in final_card["elements"]] == ["markdown"]


async def test_silent_turn_discards_prewarmed_card() -> None:
    adapter, api = make_adapter()

    async def script(consumer: GatewayStreamConsumer) -> None:
        consumer.finish("")

    try:
        await _run_turn(adapter, script)
    except AssertionError:
        # A completely empty turn may finalize without any text; that's fine as long as
        # the warmed card is removed and nothing placeholder-y is finalized.
        pass
    if api.sends():
        # If a prewarm happened, it must have been deleted, not finalized as an empty card.
        assert api.deletes(), fills(api.calls)


async def test_elapsed_heartbeat_repaints_status() -> None:
    adapter, api = make_adapter()

    async def script(consumer: GatewayStreamConsumer) -> None:
        consumer.on_card_tool("tool.started", "terminal")
        await asyncio.sleep(0.05)
        before = len(api.patches())
        # Force the 15s heartbeat without waiting 15s: age the last card frame timestamp.
        consumer._card_last_send_ts -= cs.ELAPSED_REFRESH_SECONDS + 1
        await consumer._card_maybe_elapsed_refresh()
        assert len(api.patches()) > before, "elapsed heartbeat did not patch the card"
        consumer.finish("done")

    await _run_turn(adapter, script)


async def test_card_disabled_falls_back_to_edit_transport() -> None:
    adapter, api = make_adapter(card_replies=False)

    async def script(consumer: GatewayStreamConsumer) -> None:
        consumer.on_delta("普通文本回复")
        await asyncio.sleep(0.05)
        consumer.finish("普通文本回复")

    await _run_turn(adapter, script)
    assert api.patches() == [] or all(c["msg_type"] != "interactive" for c in api.patches())
    texts = [c for c in api.calls if c["op"] in ("reply", "create") and c["msg_type"] in ("text", "post")]
    assert texts, fills(api.calls)


async def test_segment_break_keeps_one_card() -> None:
    adapter, api = make_adapter()

    async def script(consumer: GatewayStreamConsumer) -> None:
        consumer.on_delta("前言部分")
        await asyncio.sleep(0.08)
        consumer.on_delta(None)  # tool boundary / segment break
        await asyncio.sleep(0.08)
        consumer.on_delta("正文部分")
        await asyncio.sleep(0.08)
        consumer.finish("前言部分\n\n正文部分")

    await _run_turn(adapter, script)
    sends = api.sends()
    assert len(sends) == 1, fills(api.calls)
    final_card = card_json(api.patches()[-1])
    body = final_card["elements"][0]["content"]
    assert body.count("前言部分") == 1, body
    assert body.count("正文部分") == 1, body
    # blocks separated by a paragraph break, in the same card body
    assert body.index("前言部分") < body.index("正文部分"), body
    assert "\n\n" in body, body


async def test_commentary_patches_card_once() -> None:
    adapter, api = make_adapter()

    async def script(consumer: GatewayStreamConsumer) -> None:
        # With no tool waiting, narration is the primary status line; one patch.
        await asyncio.sleep(0.05)
        before = len(api.patches())
        consumer.on_card_commentary("检查一下配置")
        await asyncio.sleep(0.08)
        delta = len(api.patches()) - before
        assert delta == 1, f"expected one commentary patch, got {delta}: {fills(api.calls)}"
        statuses = [el["content"] for call in api.patches() for el in card_json(call)["elements"]
                    if el.get("tag") == "markdown" and "💬" in str(el.get("content", ""))]
        assert any("检查一下配置" in s for s in statuses), fills(api.calls)
        consumer.finish("done")

    await _run_turn(adapter, script)


async def test_long_answer_uses_card_head_and_continuation_messages() -> None:
    adapter, api = make_adapter()
    answer = "答" * (cs.CARD_BODY_MAX_CHARS + 600)

    async def script(consumer: GatewayStreamConsumer) -> None:
        consumer.on_delta(answer)
        await asyncio.sleep(0.08)
        consumer.finish(answer)

    consumer = await _run_turn(adapter, script)
    assert consumer.final_response_sent is True
    final_card = card_json(api.patches()[-1])
    card_body = "".join(el["content"] for el in final_card["elements"] if el["tag"] == "markdown")
    assert cs.CARD_TRUNCATION_NOTICE in card_body, card_body[-80:]
    assert len(card_body) <= cs.CARD_BODY_MAX_CHARS
    # The remainder arrives as a continuation CARD, not a loose text message.
    sends = api.ok_sends()
    assert len(sends) >= 2, fills(api.calls)
    tail = card_json(sends[-1])
    tail_text = "".join(
        el.get("content", "") for el in tail["elements"] if el["tag"] == "markdown"
    )
    assert "答" in tail_text and len(tail_text) >= 300, len(tail_text)
    assert "续" in tail["header"]["title"]["content"]
    assert api.text_sends() == [], fills(api.calls)


async def test_card_patch_failure_downgrades_without_losing_text() -> None:
    adapter, api = make_adapter()
    api.patch_raises = True
    api.update_fails = True

    async def script(consumer: GatewayStreamConsumer) -> None:
        consumer.on_delta("第一段")
        await asyncio.sleep(0.08)
        consumer.on_delta("，第二段")
        await asyncio.sleep(0.08)
        consumer.finish("第一段，第二段")

    consumer = await _run_turn(adapter, script)
    # Card patching failed: the legacy fallback must deliver the unseen text exactly once.
    assert consumer.already_sent is True or consumer.final_response_sent is True
    delivered = "".join(call["content"] for call in api.text_sends())
    assert "第二段" in delivered or "第一段，第二段" in delivered, fills(api.calls)


async def test_prewarm_rejection_falls_back_to_text_streaming() -> None:
    adapter, api = make_adapter()
    api.fail_interactive_sends = True

    async def script(consumer: GatewayStreamConsumer) -> None:
        consumer.on_delta("普通文本回复")
        await asyncio.sleep(0.08)
        consumer.finish("普通文本回复")

    consumer = await _run_turn(adapter, script)
    assert consumer.final_response_sent is True
    assert api.ok_sends() == [], fills(api.calls)  # no interactive card ever landed
    assert api.text_sends(), fills(api.calls)


async def test_patch_rate_floor_coalesces_tool_burst() -> None:
    adapter, api = make_adapter()

    async def script(consumer: GatewayStreamConsumer) -> None:
        for i in range(6):
            consumer.on_card_tool("tool.started", f"tool_{i % 3}")
            consumer.on_card_tool("tool.completed", f"tool_{i % 3}")
        await asyncio.sleep(0.05)
        # All six lifecycle events landed inside the 0.25s floor: at most the prewarm
        # frame exists, no burst of PATCHes.
        assert len(api.patches()) == 0, fills(api.calls)
        consumer.finish("done")

    await _run_turn(adapter, script, min_patch_interval=0.25)


async def test_card_patch_rate_limit_backs_off_without_downgrade() -> None:
    adapter, api = make_adapter()
    api.patch_rate_limit_attempts = 2  # adapter retries with backoff, then succeeds

    async def script(consumer: GatewayStreamConsumer) -> None:
        consumer.on_delta("限流测试")
        await asyncio.sleep(5.0)  # 1.2s + 2.4s backoff, then the retry succeeds
        consumer.finish("限流测试")

    consumer = await _run_turn(adapter, script)
    assert consumer._use_card_streaming is True, "rate limit must not disable the card"
    assert consumer.final_response_sent is True
    assert api.text_sends() == [], fills(api.calls)  # no fallback bubble
    assert api.patches(), fills(api.calls)


async def test_card_details_panel_tracks_tools_reasoning_and_errors() -> None:
    adapter, api = make_adapter()

    async def script(consumer: GatewayStreamConsumer) -> None:
        consumer.on_card_tool("tool.started", "terminal", "uname -a && hostname")
        await asyncio.sleep(0.12)
        panels = []
        statuses = []
        for call in api.patches():
            for el in card_json(call).get("elements") or []:
                if not isinstance(el, dict):
                    continue
                if el.get("tag") == "note":
                    text = "".join(str(x.get("content") or "") for x in el.get("elements") or [])
                    if "🧾" in text:
                        panels.append(text)
                elif el.get("tag") == "markdown":
                    statuses.append(str(el.get("content") or ""))
        joined = "\n".join(panels)
        assert "共 1 次" in joined and "💻terminal×1" in joined, fills(api.calls)
        assert "先检查配置" not in joined  # reasoning is no longer crammed into the note
        assert any("最后：terminal" in s for s in statuses), fills(api.calls)
        consumer.on_card_tool("tool.completed", "terminal", is_error=True)
        await asyncio.sleep(0.12)
        panels = []
        for call in api.patches():
            for el in card_json(call).get("elements") or []:
                if isinstance(el, dict) and el.get("tag") == "note":
                    text = "".join(str(x.get("content") or "") for x in el.get("elements") or [])
                    if "🧾" in text:
                        panels.append(text)
        joined = "\n".join(panels)
        assert "⚠️ 1 个错误" in joined and "terminal 执行失败" in joined, fills(api.calls)
        consumer.finish("done")

    await _run_turn(adapter, script)


async def test_clarify_prompt_renders_interactive_card() -> None:
    adapter, api = make_adapter()
    from tools import clarify_gateway

    clarify_id = "clarify-card-1"
    session_key = "sess-clarify-1"
    clarify_gateway.register(clarify_id, session_key, "选哪个？", ["A 方案", "B 方案"])
    try:
        result = await adapter.send_clarify(
            "oc_test", "选哪个？", ["A 方案", "B 方案"], clarify_id, session_key,
        )
        assert result.success, result.error
        sends = api.sends()
        assert len(sends) == 1, fills(api.calls)
        card = card_json(sends[0])
        text = json.dumps(card, ensure_ascii=False)
        assert "选哪个？" in text
        assert "hermes_clarify_action" in text
        assert clarify_id in text
        assert "A 方案" in text and "B 方案" in text
        action = next(el for el in card["elements"] if el.get("tag") == "action")
        labels = [btn.get("text", {}).get("content") for btn in action.get("actions") or []]
        assert "✏️ 其他" in labels and "⏭️ 跳过" in labels, labels
    finally:
        clarify_gateway.clear_session(session_key)


async def test_clarify_card_action_resolves_pending_prompt() -> None:
    adapter, api = make_adapter()
    from types import SimpleNamespace
    from tools import clarify_gateway

    clarify_id = "clarify-card-2"
    session_key = "sess-clarify-2"
    clarify_gateway.register(clarify_id, session_key, "选哪个？", ["A", "B"])
    try:
        result = await adapter.send_clarify("oc_test", "选哪个？", ["A", "B"], clarify_id, session_key)
        assert result.success, result.error
        event = SimpleNamespace(
            operator=SimpleNamespace(open_id="ou_test"),
            context=SimpleNamespace(open_chat_id="oc_test"),
        )
        adapter._handle_clarify_card_action(
            event=event,
            action_value={"hermes_clarify_action": "choice", "clarify_id": clarify_id, "answer": "B"},
        )
        assert clarify_gateway.get_pending(clarify_id) is None
        assert clarify_gateway.wait_for_response(clarify_id, 0.1) == "B"
    finally:
        clarify_gateway.clear_session(session_key)


async def test_edit_message_folds_final_into_live_card_without_metadata() -> None:
    adapter, api = make_adapter()
    payload = {
        "schema": 1, "title": "Hermes", "body": "旧正文", "status": "✍️ 正在生成回复…",
        "details": "", "footer": "", "final": False, "template": "turquoise",
    }
    sent = await adapter.send("oc_test", "旧正文", metadata={"hermes_card_stream": payload})
    assert sent.success and sent.message_id
    updated = await adapter.edit_message("oc_test", sent.message_id, "完整答案正文", finalize=True)
    assert updated.success, updated.error
    # Only the original card message exists; the final answer was folded into it.
    assert len(api.sends()) == 1, fills(api.calls)
    card = card_json(api.patches()[-1])
    text = json.dumps(card, ensure_ascii=False)
    assert "完整答案正文" in text
    assert card["header"]["template"] == cs.HEADER_TEMPLATE_FINAL


async def test_clarify_multi_select_toggle_and_submit() -> None:
    adapter, api = make_adapter()
    from types import SimpleNamespace
    from tools import clarify_gateway

    clarify_id = "clarify-multi-1"
    session_key = "sess-clarify-multi"
    clarify_gateway.register(clarify_id, session_key, "选哪些？", ["A", "B", "C"], multi_select=True)
    try:
        result = await adapter.send_clarify(
            "oc_test", "选哪些？", ["A", "B", "C"], clarify_id, session_key,
        )
        assert result.success, result.error
        card = card_json(api.sends()[-1])
        action = next(el for el in card["elements"] if el.get("tag") == "action")
        values = [btn.get("value") for btn in action["actions"]]
        assert any(v.get("hermes_clarify_action") == "toggle" for v in values)
        assert any(v.get("hermes_clarify_action") == "submit" for v in values)
        event = SimpleNamespace(
            operator=SimpleNamespace(open_id="ou_test"),
            context=SimpleNamespace(open_chat_id="oc_test"),
        )
        for answer in ("A", "C"):
            adapter._handle_clarify_card_action(
                event=event,
                action_value={"hermes_clarify_action": "toggle", "clarify_id": clarify_id, "answer": answer},
            )
        assert adapter._clarify_state[clarify_id]["selected"] == ["A", "C"]
        adapter._handle_clarify_card_action(
            event=event,
            action_value={"hermes_clarify_action": "submit", "clarify_id": clarify_id},
        )
        assert clarify_gateway.get_pending(clarify_id) is None
        assert json.loads(clarify_gateway.wait_for_response(clarify_id, 0.1)) == ["A", "C"]
    finally:
        clarify_gateway.clear_session(session_key)


async def test_slash_confirm_renders_three_buttons() -> None:
    adapter, api = make_adapter()
    result = await adapter.send_slash_confirm(
        "oc_test", "Confirm /new", "This starts a fresh session", "sess-slash", "7",
    )
    assert result.success, result.error
    card = card_json(api.sends()[-1])
    action = next(el for el in card["elements"] if el.get("tag") == "action")
    values = [btn.get("value") for btn in action["actions"]]
    assert [v.get("hermes_slash_confirm") for v in values] == ["once", "always", "cancel"]
    assert all(v.get("confirm_id") == "7" for v in values)
    assert adapter._slash_confirm_state["7"]["session_key"] == "sess-slash"


async def test_table_final_card_renders_column_set() -> None:
    adapter, api = make_adapter()
    body = (
        "| 主机 | 状态 | 耗时 |\n"
        "|---|---|---|\n"
        "| 82 | 通过 | 1s |\n"
        "| 80 | 通过 | 2s |\n"
    )
    result = await adapter.send("oc_test", body, metadata={"is_final_response": True})
    assert result.success, result.error
    card = card_json(api.sends()[-1])
    tags = [el["tag"] for el in card["elements"]]
    assert "column_set" in tags, tags
    text = json.dumps(card, ensure_ascii=False)
    assert "82" in text and "通过" in text
    assert "| 主机 |" not in text


async def test_normal_final_send_folds_into_open_live_card() -> None:
    adapter, api = make_adapter()
    payload = {
        "schema": 1, "title": "Hermes", "body": "部分答案", "status": "✍️ 正在生成回复…",
        "details": "", "stats": {}, "chat_summary": "", "footer": "", "final": False,
        "template": "turquoise",
    }
    sent = await adapter.send("oc_test", "部分答案", metadata={"hermes_card_stream": payload})
    assert sent.success and sent.message_id
    folded = await adapter.send(
        "oc_test", "⚠️ Provider temporarily unavailable — retries exhausted",
        metadata={"is_final_response": True},
    )
    assert folded.success, folded.error
    assert len(api.sends()) == 1, fills(api.calls)  # no second card/bubble
    card = card_json(api.patches()[-1])
    text = json.dumps(card, ensure_ascii=False)
    assert "Provider temporarily unavailable" in text
    assert card["header"]["template"] == cs.HEADER_TEMPLATE_ERROR


async def test_card_replies_setting_parsing() -> None:
    from plugins.platforms.feishu.adapter import FeishuAdapter
    assert FeishuAdapter._load_settings({"card_replies": True}).card_replies is True
    assert FeishuAdapter._load_settings({"card_replies": "true"}).card_replies is True
    assert FeishuAdapter._load_settings({"card_replies": "1"}).card_replies is True
    assert FeishuAdapter._load_settings({"card_replies": False}).card_replies is False
    assert FeishuAdapter._load_settings({}).card_replies is False


TESTS = [
    test_non_streamed_final_renders_card,
    test_non_streamed_final_over_card_budget_falls_back_to_text,
    test_footer_folds_into_live_card,
    test_card_replies_setting_parsing,
    test_one_card_per_turn_with_status_and_narration,
    test_commentary_patches_card_once,
    test_segment_break_keeps_one_card,
    test_long_answer_uses_card_head_and_continuation_messages,
    test_card_patch_failure_downgrades_without_losing_text,
    test_prewarm_rejection_falls_back_to_text_streaming,
    test_patch_rate_floor_coalesces_tool_burst,
    test_card_patch_rate_limit_backs_off_without_downgrade,
    test_card_details_panel_tracks_tools_reasoning_and_errors,
    test_clarify_prompt_renders_interactive_card,
    test_clarify_card_action_resolves_pending_prompt,
    test_edit_message_folds_final_into_live_card_without_metadata,
    test_clarify_multi_select_toggle_and_submit,
    test_slash_confirm_renders_three_buttons,
    test_table_final_card_renders_column_set,
    test_normal_final_send_folds_into_open_live_card,
    test_tool_only_turn_final_body,
    test_silent_turn_discards_prewarmed_card,
    test_elapsed_heartbeat_repaints_status,
    test_card_disabled_falls_back_to_edit_transport,
]


def main() -> int:
    failures = 0
    for fn in TESTS:
        try:
            asyncio.run(fn())
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print(f"{len(TESTS) - failures}/{len(TESTS)} integration tests passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
