#!/usr/bin/env python3
"""Summarize Feishu card transport health from the metrics JSONL file.

Usage:
    python tools/card_health.py
    python tools/card_health.py --path ~/.hermes/logs/feishu_card_metrics.jsonl --since 60
    python tools/card_health.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=os.path.join(os.path.expanduser("~"), ".hermes", "logs",
                                                   "feishu_card_metrics.jsonl"))
    ap.add_argument("--since", type=float, default=0, help="only events from the last N minutes")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    since_ts = time.time() - max(0.0, args.since) * 60
    counts: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    last = 0.0
    total = 0
    try:
        with open(args.path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                ts = float(item.get("ts") or 0)
                if ts < since_ts:
                    continue
                total += 1
                last = max(last, ts)
                event = str(item.get("event") or "unknown")
                counts[event] += 1
                if item.get("error"):
                    errors[str(item["error"])[:160]] += 1
    except FileNotFoundError:
        print(f"no metrics file yet: {args.path}")
        return 0
    result = {
        "path": args.path,
        "events": dict(counts),
        "errors": dict(errors.most_common(5)),
        "total": total,
        "last_event": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last)) if last else "",
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"card metrics: {args.path}")
    print(f"  events={total} last={result['last_event'] or '-'}")
    for event, count in counts.most_common():
        print(f"  {event}: {count}")
    for error, count in errors.most_common(5):
        print(f"  error×{count}: {error}")
    # Exit non-zero when the transport is clearly unhealthy in the window.
    unhealthy = counts.get("card_patch_failed", 0) + counts.get("card_send_failed", 0)
    return 1 if unhealthy and unhealthy >= max(3, total // 2) else 0


if __name__ == "__main__":
    sys.exit(main())
