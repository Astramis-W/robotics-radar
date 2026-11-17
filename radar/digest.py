"""Недельный дайджест: что важного произошло, по данным из базы событий."""
from __future__ import annotations

import json
from datetime import timedelta

from .config import Taxonomy
from .llm import LLM
from .store import Store
from .util import now, now_iso, parse_date

SYSTEM = """Ты — редактор отраслевого дайджеста по робототехнике для команды робототехнической компании.
Тебе дают события за неделю (JSON). Напиши дайджест по-русски в Markdown, до 350 слов.

Структура (заголовки второго уровня, пропускай раздел, если писать не о чем):
## Главное
## Деньги
## Продукты и внедрения
## Россия
## Наши ниши
## На что обратить внимание

Правила: только факты из переданных событий, с названиями компаний и цифрами; никаких выдуманных
деталей и оценок рынка «из головы». Короткие абзацы или списки. В разделе «Наши ниши» — события
с непустым полем focus. В последнем разделе — один-три вывода, что это значит для команды,
которая делает сервисных роботов (фасады, уборка полов, резервуары)."""


def run_digest(store: Store, llm: LLM, tax: Taxonomy, *, days: int = 7) -> dict:
    end = now().date()
    start = end - timedelta(days=days)
    week = [e for e in store.events if (d := parse_date(e.get("date"))) and start <= d <= end]
    if len(week) < 3:
        return {"digest": None, "note": f"событий за неделю: {len(week)} — дайджест не нужен"}
    cats = {c["id"]: c["label"] for c in tax.categories}
    types = {t["id"]: t["label"] for t in tax.event_types}
    focus = {f["id"]: f["label"] for f in tax.focus}
    week.sort(key=lambda e: (-e["importance"], e["date"]))
    rows = [{
        "title": e["title"], "summary": e["summary"], "type": types.get(e["type"], e["type"]),
        "company": e["company"], "country": e["country"], "date": e["date"],
        "categories": [cats.get(c, c) for c in e["categories"]],
        "amount_usd": (e.get("amount") or {}).get("usd"),
        "importance": e["importance"], "focus": [focus.get(f, f) for f in e["focus"]],
    } for e in week[:120]]
    text = llm.text(SYSTEM, f"Неделя {start.isoformat()} — {end.isoformat()}. События:\n"
                    + json.dumps(rows, ensure_ascii=False)).strip()
    digest = {"start": start.isoformat(), "end": end.isoformat(), "created": now_iso(),
              "events": len(week), "markdown": text}
    store.digests = [d for d in store.digests if d.get("end") != digest["end"]] + [digest]
    store.digests = store.digests[-52:]
    return {"digest": digest}
