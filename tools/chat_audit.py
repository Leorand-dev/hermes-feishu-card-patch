#!/usr/bin/env python3
"""Audit the recent Feishu chat presentation (before/after the streaming-card patch).

Lists the newest messages in a chat and reports, per bot-card group (reply root or
standalone message): how many card bubbles one agent turn left behind, whether cards
were patched in place, and the text/post bubble count.

    PYTHONPATH=. ~/.hermes/hermes-agent/.venv/bin/python tools/chat_audit.py --limit 50 --json

Credentials: FEISHU_APP_ID / FEISHU_APP_SECRET (env or ~/.hermes/.env).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict


def load_env(path: str) -> None:
    """Load only FEISHU_* keys from the Hermes .env (nothing else is needed here)."""
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", default="", help="chat id (default: feishu home_channel)")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    load_env(os.path.expanduser("~/.hermes/.env"))
    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import ListMessageRequest
    except Exception as exc:  # pragma: no cover
        print(f"lark_oapi unavailable: {exc}")
        return 2
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    if not (app_id and app_secret):
        print("FEISHU_APP_ID / FEISHU_APP_SECRET missing")
        return 2

    chat = args.chat
    if not chat:
        try:
            from gateway.run import _load_gateway_config
            cfg = _load_gateway_config()
            chat = (((cfg.get("platforms") or {}).get("feishu") or {}).get("home_channel") or {}).get("chat_id") or ""
        except Exception:
            chat = ""
    if not chat:
        print("no chat id: pass --chat oc_xxx")
        return 2

    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).log_level(lark.LogLevel.WARNING).build()
    req = (ListMessageRequest.builder()
           .container_id_type("chat").container_id(chat)
           .sort_type("ByCreateTimeDesc").page_size(min(50, max(1, args.limit))).build())
    resp = client.im.v1.message.list(req)
    if not resp.success():
        print(f"message.list failed: code={resp.code} msg={resp.msg}")
        return 1
    items = list(getattr(resp.data, "items", None) or [])
    items.sort(key=lambda it: int(getattr(it, "create_time", "0") or 0))

    groups: dict[str, list] = defaultdict(list)
    for it in items:
        sender_type = getattr(getattr(it, "sender", None), "sender_type", "")
        if sender_type not in ("app", "bot"):
            continue
        root = (getattr(it, "root_id", None) or getattr(it, "parent_id", None)
                or getattr(it, "message_id", ""))
        groups[root].append(it)

    card_groups = []
    for root, arr in groups.items():
        kinds = Counter(getattr(it, "msg_type", "") for it in arr)
        if not kinds.get("interactive"):
            continue
        # updated cards were patched in place
        patched = sum(1 for it in arr if int(getattr(it, "update_time", "0") or 0)
                      > int(getattr(it, "create_time", "0") or 0) + 500)
        card_groups.append({
            "root": root,
            "cards": kinds.get("interactive", 0),
            "text": kinds.get("text", 0),
            "post": kinds.get("post", 0),
            "patched_in_place": patched,
            "first_create": min(int(getattr(it, "create_time", "0") or 0) for it in arr),
        })
    card_groups.sort(key=lambda g: g["first_create"])

    msg_types = Counter(getattr(it, "msg_type", "") for it in items)
    summary = {
        "chat": chat,
        "messages_listed": len(items),
        "msg_type_counts": dict(msg_types),
        "bot_turn_groups": len(card_groups),
        "max_cards_per_turn": max((g["cards"] for g in card_groups), default=0),
        "one_card_turns": sum(1 for g in card_groups if g["cards"] == 1),
        "turns": card_groups[-8:],
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"chat {chat}: {len(items)} messages; types={dict(msg_types)}")
        print(f"bot turns with cards: {len(card_groups)}; "
              f"one-card turns: {summary['one_card_turns']}; "
              f"max cards in one turn: {summary['max_cards_per_turn']}")
        for g in card_groups[-8:]:
            print(f"  turn root={g['root'][:24]} cards={g['cards']} text={g['text']} "
                  f"patched_in_place={g['patched_in_place']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
