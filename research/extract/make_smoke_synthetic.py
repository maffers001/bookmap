"""Generate a tiny synthetic extract session for pipeline smoke tests."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from research.config import load_config


def main() -> None:
    cfg = load_config()
    raw = Path(cfg["raw_dir"]) / "smoke_synthetic"
    raw.mkdir(parents=True, exist_ok=True)

    uk = ZoneInfo("Europe/London")
    start = datetime(2026, 9, 18, 14, 35, tzinfo=uk)
    t0 = int(start.timestamp() * 1e9)
    bar = 250_000_000  # 250ms

    trades = []
    price = 20000.0
    # 40 bars: early bars sell pushes price down hard; later bars heavy sell, no progress
    for i in range(40):
        ts = t0 + i * bar
        sell_size = 30 + i * 5
        # Within-bar path: several sells
        if i < 15:
            # strong downward progress
            for k in range(4):
                price -= 0.50  # 2 ticks each
                trades.append(_sell(ts + k * 10_000_000, price, sell_size // 4, i, k))
        else:
            # high sell effort, flat price (failure to progress)
            for k in range(4):
                trades.append(_sell(ts + k * 10_000_000, price, sell_size // 4, i, k))
            # small bounce at end for forward outcome
            if i >= 36:
                price += 1.0

    with open(raw / "trades.ndjson", "w", encoding="utf-8") as f:
        for r in trades:
            f.write(json.dumps(r) + "\n")

    with open(raw / "depth.ndjson", "w", encoding="utf-8") as f:
        for i in range(20):
            f.write(json.dumps({
                "ts_ns": t0 + i * bar,
                "alias": "NQZ6",
                "role": "NQ",
                "symbol": "NQZ6",
                "is_bid": True,
                "price_ticks": int(price / 0.25) - 4,
                "size": 200,
            }) + "\n")

    (raw / "mbo.ndjson").write_text("", encoding="utf-8")
    meta = {
        "session_id": "smoke_synthetic",
        "finished": True,
        "trade_count": len(trades),
        "depth_count": 20,
        "mbo_count": 0,
        "mbo_seen": False,
        "instruments": [{"alias": "NQZ6", "role": "NQ", "symbol": "NQZ6", "pips": 0.25}],
    }
    (raw / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {raw} trades={len(trades)}")


def _sell(ts, price, size, i, k):
    return {
        "ts_ns": ts,
        "alias": "NQZ6",
        "role": "NQ",
        "symbol": "NQZ6",
        "price": price,
        "size": max(1, size),
        "is_buy_aggressor": False,
        "aggressor_order_id": f"S{i}_{k}",
        "is_exec_start": True,
        "is_exec_end": True,
    }


if __name__ == "__main__":
    main()
