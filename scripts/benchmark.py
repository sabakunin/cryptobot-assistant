"""Synthetic local benchmark; no Telegram connection and no financial operations."""

import json
import statistics
import time

from sniper.parser import parse_offer

CARD = """Объявление #O2713926
Example продаёт USDT за RUB.
Цена за 1 USDT: 89.87 RUB
Доступный объём: 53 USDT
Способ оплаты: СБП
Условия сделки:
Одним платежом. ПДФ чек.
"""

samples = []
for _ in range(10000):
    start = time.perf_counter_ns()
    parse_offer(CARD, ["Купить 53.088984 USDT"])
    samples.append((time.perf_counter_ns() - start) / 1e6)
samples.sort()
print(
    json.dumps(
        {
            "kind": "synthetic parser only",
            "count": len(samples),
            "mean_ms": statistics.mean(samples),
            "p95_ms": samples[int(len(samples) * 0.95)],
        },
        indent=2,
    )
)
