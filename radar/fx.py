"""Пересчёт сумм в доллары по текущему курсу (open.er-api.com, без ключа)."""
from __future__ import annotations

import logging

import httpx

from .util import days_between, today

log = logging.getLogger("radar.fx")

URL = "https://open.er-api.com/v6/latest/USD"
ALIASES = {"₽": "RUB", "РУБ": "RUB", "RUR": "RUB", "$": "USD", "US$": "USD", "€": "EUR", "£": "GBP",
           "RMB": "CNY", "ЮАНЬ": "CNY", "ЕВРО": "EUR", "ДОЛЛ": "USD"}


class Rates:
    def __init__(self, cache: dict | None = None) -> None:
        self.cache = cache if cache is not None else {}

    def refresh(self) -> None:
        if self.cache.get("rates") and (days_between(self.cache.get("date"), today()) or 0) < 1:
            return
        try:
            data = httpx.get(URL, timeout=15).json()
            if data.get("result") == "success":
                self.cache.clear()
                self.cache.update({"date": today(), "rates": data["rates"]})
        except Exception as exc:  # noqa: BLE001 — без курса просто не будет пересчёта
            log.warning("курсы валют недоступны: %s", exc)

    def to_usd(self, amount: float | None, currency: str | None) -> float | None:
        if not amount or amount <= 0:
            return None
        cur = (currency or "").strip().upper()
        cur = ALIASES.get(cur, cur)
        if cur == "USD":
            return round(float(amount), 2)
        rate = (self.cache.get("rates") or {}).get(cur)
        return round(float(amount) / rate, 2) if rate else None
