"""Уведомления в Telegram-чат команды (необязательно): важные события и недельный дайджест."""
from __future__ import annotations

import html
import logging
import re

import httpx

from .config import Settings

log = logging.getLogger("radar.notify")
LIMIT = 3900


def send(settings: Settings, text: str) -> bool:
    if not (settings.tg_token and settings.tg_chat):
        return False
    ok = True
    for chunk in _chunks(text):
        try:
            r = httpx.post(f"https://api.telegram.org/bot{settings.tg_token}/sendMessage",
                           json={"chat_id": settings.tg_chat, "text": chunk, "parse_mode": "HTML",
                                 "disable_web_page_preview": True}, timeout=20)
            if r.status_code != 200:
                ok = False
                log.warning("Telegram: HTTP %s %s", r.status_code, r.text[:200])
        except Exception as exc:  # noqa: BLE001
            ok = False
            log.warning("Telegram: %s", exc)
    return ok


def _chunks(text: str) -> list[str]:
    parts, current = [], ""
    for block in text.split("\n\n"):
        if len(current) + len(block) + 2 > LIMIT and current:
            parts.append(current)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    return parts + ([current] if current else [])


def events_message(events: list[dict], min_importance: int) -> str:
    picked = [e for e in events if e["importance"] >= min_importance or e["focus"]]
    if not picked:
        return ""
    picked.sort(key=lambda e: (-bool(e["focus"]), -e["importance"]))
    blocks = [f"<b>Радар: новых важных событий — {len(picked)}</b>"]
    for e in picked[:10]:
        line = f"<b>{html.escape(e['title'])}</b>"
        if e["focus"]:
            line = "★ " + line
        body = html.escape(e["summary"])
        url = e["sources"][0]["url"] if e["sources"] else ""
        link = f'\n<a href="{html.escape(url, quote=True)}">Источник</a>' if url else ""
        blocks.append(f"{line}\n{body}{link}")
    return "\n\n".join(blocks)


def digest_message(digest: dict) -> str:
    md = html.escape(digest["markdown"])
    md = re.sub(r"^#{1,3}\s*(.+)$", r"<b>\1</b>", md, flags=re.M)
    md = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", md)
    md = re.sub(r"^[-*]\s+", "• ", md, flags=re.M)
    return f"<b>Радар робототехники: неделя {digest['start']} — {digest['end']}</b>\n\n{md}"
