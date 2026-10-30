#!/usr/bin/env python3
"""Live Feishu proof for the streaming-card patch (uses the real bot credentials).

Instantiates the PATCHED FeishuAdapter, sends one real interactive card built by
``plugins/platforms/feishu/card_stream.py`` to the configured chat, patches it
through the adapter's real ``edit_message``/``send_footer`` path, verifies the
card JSON through the message-list API, and then recalls the probe card.

Usage (from the Hermes checkout root, on the host):

    PYTHONPATH=. ~/.hermes/hermes-agent/.venv/bin/python \
        tools/live_card_probe.py --chat oc_xxx

Credentials: FEISHU_APP_ID / FEISHU_APP_SECRET (env or ~/.hermes/.env).
Exit codes: 0 ok, 1 failure, 2 missing credentials/deps.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import traceback
import uuid
from typing import Any

MARKER = "[probe] hermes streaming-card"


def load_env(path: str) -> None:
    """Load only FEISHU_* keys from the Hermes .env (the probe needs nothing else).

    Deliberately narrow: dumping every unrelated key from the gateway env into the
    probe process can change interpreter/library resolution in hard-to-diagnose ways.
    """
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key.startswith("FEISHU_"):
                continue
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def build_payloads(cs) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    turn = cs.CardStreamTurn(title="Hermes")
    turn.mark_prewarmed()
    prewarm = turn.render_payload(final=False)
    turn.on_step(7)
    for name in ("terminal", "read_file", "write_file"):
        turn.on_tool_started(name, detail="probe")
        turn.on_tool_completed()
    turn.on_narration("检查配置")
    turn.on_notice("⏳ Provider temporarily unavailable — retrying in 18s")
    mid = turn.render_payload(final=False)
    turn.on_answer_started()
    turn.set_body(
        f"{MARKER}\n\n"
        "| 主机 | 状态 |\n"
        "|---|---|\n"
        "| 82 | OK |\n\n"
        "最终答案正文（流式卡片联调）"
    )
    turn.finalize(footer="deepseek-flash · 42% · cache 87% · 110.8 tok/s · 3m12s")
    final = turn.render_payload(final=True)
    return prewarm, mid, final


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", default="", help="chat id (default: platforms.feishu.home_channel.chat_id)")
    ap.add_argument("--env-file", default=os.path.expanduser("~/.hermes/.env"))
    ap.add_argument("--keep", action="store_true", help="keep the probe card (do not recall)")
    args = ap.parse_args()

    load_env(args.env_file)
    try:
        from gateway.config import PlatformConfig
        from plugins.platforms.feishu import adapter as feishu_adapter_module
        from plugins.platforms.feishu.adapter import FeishuAdapter
        from plugins.platforms.feishu import card_stream as cs
        if not feishu_adapter_module._load_lark_oapi():
            print("lark_oapi not importable in this interpreter")
            return 2
    except Exception as exc:  # pragma: no cover
        print(f"import failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 2
    if not (os.environ.get("FEISHU_APP_ID") and os.environ.get("FEISHU_APP_SECRET")):
        print("FEISHU_APP_ID / FEISHU_APP_SECRET missing")
        return 2

    chat = args.chat
    if not chat:
        try:
            from gateway.run import _load_gateway_config
            cfg = _load_gateway_config()
            home = (cfg.get("platforms") or {}).get("feishu", {}).get("home_channel") or {}
            chat = home.get("chat_id") or ""
        except Exception:
            chat = ""
    if not chat:
        print("no chat id: pass --chat oc_xxx")
        return 2

    adapter = FeishuAdapter(PlatformConfig(enabled=True, extra={"card_replies": True}))
    adapter._prepare_client()
    if not adapter._client:
        print("Feishu client construction failed")
        return 1

    prewarm, mid, final = build_payloads(cs)
    message_id = ""
    ok = False
    try:
        result = await adapter.send(chat, "", metadata={"hermes_card_stream": prewarm})
        print(f"send prewarm card: success={result.success} id={result.message_id} err={result.error}")
        if not result.success or not result.message_id:
            return 1
        message_id = str(result.message_id)
        meta = {"hermes_card_stream": mid}
        r2 = await adapter.edit_message(chat, message_id, "", finalize=False, metadata=meta)
        print(f"patch mid card:    success={r2.success} err={r2.error}")
        mid_ok = False
        final_ok = False

        def fetch_card_content() -> str:
            from lark_oapi.api.im.v1 import GetMessageRequest
            got = adapter._client.im.v1.message.get(
                GetMessageRequest.builder().message_id(message_id).build(),
            )
            got_items = list(getattr(got.data, "items", None) or [])
            return str(getattr(getattr(got_items[0], "body", None), "content", "")) if got_items else ""

        try:
            raw = fetch_card_content()
            mid_ok = (
                "🧾 运行详情" in raw
                and "共 3 次" in raw
                and "terminal×1" in raw
                and "Provider temporarily unavailable" in raw
            )
            print(f"mid details/status: {'OK' if mid_ok else 'MISSING'}")
            if not mid_ok:
                print("  raw head:", raw[:500].replace("\n", " "))
        except Exception as exc:  # noqa: BLE001
            print(f"mid details check failed: {exc}")
        r3 = await adapter.edit_message(chat, message_id, final["body"], finalize=True,
                                        metadata={"hermes_card_stream": final})
        print(f"patch final card:  success={r3.success} err={r3.error}")
        r4 = await adapter.send_footer(chat, final["footer"])
        print(f"patch footer note: success={r4.success} err={r4.error}")
        try:
            raw = fetch_card_content()
            final_ok = (
                "✅ 完成 · 3 个工具" in raw
                and "🧾" in raw
                and "主要工具" in raw
                and "terminal×1" in raw
                and "3m12s" in raw  # footer latency: the only duration shown
                and "[probe]" in raw
                and "主机" in raw and "82" in raw  # pipe table became a column_set
                and "|---|---|" not in raw
            )
            print(f"final summary/details: {'OK' if final_ok else 'MISSING'}")
            if not final_ok:
                print("  raw head:", raw[:900].replace("\n", " "))
        except Exception as exc:  # noqa: BLE001
            print(f"final summary check failed: {exc}")
        ok = all(r.success for r in (result, r2, r3, r4)) and mid_ok and final_ok
    finally:
        if message_id and not args.keep:
            deleted = await adapter.delete_message(chat, message_id)
            print(f"recall probe card: {deleted}")
        try:
            adapter._shutdown_sdk_executor()
        except Exception:
            pass
    print("LIVE CARD PROBE:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
