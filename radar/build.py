"""Сборка данных для страницы: data/*  ->  site/data/radar.json"""
from __future__ import annotations

from datetime import timedelta

from . import __version__
from .config import Settings, Taxonomy, load_yaml
from .store import Store
from .util import now, now_iso, parse_date, write_json

EVENTS_WINDOW_DAYS = 550
EVENTS_MAX = 6000


def _apply_overrides(companies: dict[str, dict], overrides: dict) -> dict[str, str]:
    """Ручные правки побеждают автоматику. Возвращает карту склейки дублей {откуда: куда}."""
    merges: dict[str, str] = {}
    for cid, patch in (overrides.get("companies") or {}).items():
        if cid not in companies or not isinstance(patch, dict):
            continue
        target = patch.get("merge_into")
        if target and target in companies and target != cid:
            merges[cid] = target
            continue
        companies[cid].update({k: v for k, v in patch.items() if k != "merge_into"})
    return merges


def build(store: Store, tax: Taxonomy, settings: Settings) -> dict:
    companies = {cid: dict(c) for cid, c in store.companies.items()}
    merges = _apply_overrides(companies, load_yaml("overrides.yaml"))
    for src, dst in merges.items():
        dup = companies.pop(src)
        names = [dup["name"]] + list(dup.get("aliases") or [])
        companies[dst]["aliases"] = list(dict.fromkeys(list(companies[dst].get("aliases") or []) + names))
    remap = lambda cid: merges.get(cid, cid)  # noqa: E731

    cutoff = (now() - timedelta(days=EVENTS_WINDOW_DAYS)).date()
    events = []
    for e in store.events:
        d = parse_date(e.get("date"))
        if not d or d < cutoff:
            continue
        e = dict(e)
        e["company_id"] = remap(e.get("company_id") or "")
        if e["company_id"] and e["company_id"] in companies:
            if companies[e["company_id"]].get("hidden"):
                continue
            e["company"] = companies[e["company_id"]]["name"]
            e["country"] = companies[e["company_id"]].get("country") or e.get("country") or ""
        e.pop("created", None)
        e.pop("updated", None)
        events.append(e)
    events.sort(key=lambda e: (e["date"], e["importance"]), reverse=True)
    events = events[:EVENTS_MAX]

    for c in companies.values():
        c["events"] = 0
        c["funding_seen_usd"] = 0.0
        c["last_event"] = ""
    for e in events:
        c = companies.get(e["company_id"])
        if not c:
            continue
        c["events"] += 1
        c["last_event"] = max(c["last_event"], e["date"])
        if e["type"] == "funding" and (e.get("amount") or {}).get("usd"):
            c["funding_seen_usd"] += e["amount"]["usd"]

    products = []
    for p in store.products.values():
        p = dict(p)
        p["company_id"] = remap(p["company_id"])
        owner = companies.get(p["company_id"])
        if not owner or owner.get("hidden"):
            continue
        p["company"] = owner["name"]
        p["country"] = owner.get("country") or ""
        products.append(p)
    products.sort(key=lambda p: p.get("updated") or p.get("first_seen") or "", reverse=True)

    last_collect = next((r for r in reversed(store.runs) if r.get("sources")), None)
    month_ago = (now() - timedelta(days=30)).strftime("%Y-%m-%d")
    cost_30d = round(sum((r.get("llm") or {}).get("cost_usd", 0) for r in store.runs
                         if (r.get("ts") or "")[:10] >= month_ago), 2)
    payload = {
        "meta": {
            "generated": now_iso(),
            "version": __version__,
            "counts": {"companies": sum(1 for c in companies.values() if not c.get("hidden")),
                       "products": len(products), "events": len(events), "queue": len(store.queue)},
            "cost_30d_usd": cost_30d,
            "sources": (last_collect or {}).get("sources") or [],
            "sources_checked": (last_collect or {}).get("ts") or "",
            "model": settings.model_fast if settings.provider == "anthropic" else (settings.openai_model or ""),
        },
        "taxonomy": tax.public(),
        "companies": sorted((c for c in companies.values() if not c.get("hidden")),
                            key=lambda c: c["name"].lower()),
        "products": products,
        "events": events,
        "digests": list(reversed(store.digests[-12:])),
    }
    write_json(settings.site_dir / "data" / "radar.json", payload, compact=True)
    return payload["meta"]["counts"]
