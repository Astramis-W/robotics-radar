"""Запуск:  python -m radar <команда>

  check    проверить источники (ничего не пишет, модель не вызывает)
  run      обычный прогон: сбор -> разбор моделью -> сборка страницы -> уведомления
  weekly   раз в неделю: обогащение карточек + дайджест + сборка страницы
  review   пересмотреть уже записанные события по текущим правилам релевантности
  collect | extract | enrich | digest | build | seed — те же шаги по отдельности
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import notify
from .build import build
from .collect import collect
from .config import Settings, Taxonomy, load_yaml
from .digest import run_digest
from .enrich import run_enrich
from .entities import load_seed
from .extract import run_extract
from .fx import Rates
from .llm import LLM
from .review import run_review
from .store import Store
from .util import now_iso

log = logging.getLogger("radar")


def cmd_check(_args) -> int:
    report = collect(None, load_yaml("sources.yaml"), dry_run=True)
    bad = 0
    for s in report["sources"]:
        if s["error"]:
            mark = "ОШИБКА"
        elif s["fetched"] or s["kind"] == "gnews":      # пустая выдача узкого поиска — не поломка
            mark = "OK"
        else:
            mark = "ПУСТО"
        bad += mark != "OK"
        print(f"{mark:7} {s['fetched']:4}  {s['source']}" + (f"  — {s['error']}" if s["error"] else ""))
    print(f"\nИсточников: {len(report['sources'])}, с проблемами: {bad}")
    return 1 if bad == len(report["sources"]) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="radar", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["check", "run", "weekly", "review", "collect", "extract",
                                            "enrich", "digest", "build", "seed"])
    parser.add_argument("--limit", type=int, help="сколько новостей или компаний обработать за раз")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.command == "check":
        return cmd_check(args)

    settings, tax = Settings(), Taxonomy()
    store = Store(settings.data_dir)
    rates = Rates(store.fx)
    llm = LLM(settings)
    record: dict = {"ts": now_iso(), "cmd": args.command, "errors": []}
    cmd = args.command

    if not store.companies or cmd == "seed":
        added = load_seed(store.companies, load_yaml("seed_companies.yaml"))
        if added:
            log.info("стартовый список: добавлено компаний — %d", added)

    if cmd in ("run", "collect"):
        sources = load_yaml("sources.yaml")
        got = collect(store, sources)
        record.update(sources=got["sources"], new_items=got["new_items"])
        record["errors"] += got["errors"]
        log.info("сбор: новых записей — %d, источников с ошибкой — %d", got["new_items"], len(got["errors"]))

    new_events: list[dict] = []
    if cmd in ("run", "extract"):
        max_age = int((load_yaml("sources.yaml").get("settings") or {}).get("max_age_days", 7))
        got = run_extract(store, llm, tax, settings, rates, max_age_days=max_age, limit=args.limit)
        new_events = got.pop("new_events")
        record["errors"] += got.pop("errors")
        record.update(got)
        log.info("разбор: обработано %d, релевантных %d, новых событий %d, склеено %d, новых компаний %d, "
                 "в очереди %d", got["processed"], got["relevant"], got["events_new"],
                 got["events_merged"], got["companies_new"], got["left_in_queue"])

    if cmd == "review":
        got = run_review(store, llm, tax)
        record["errors"] += got.pop("errors")
        record.update(got)
        log.info("пересмотр: проверено %d, убрано %d, важность изменена у %d, карточек убрано %d",
                 got["reviewed"], got["removed"], got["rescored"], got["companies_removed"])

    if cmd in ("weekly", "enrich"):
        got = run_enrich(store, llm, tax, settings, rates, limit=args.limit)
        record["errors"] += got.pop("errors")
        record.update(got)
        log.info("обогащение: обновлено %d, не найдено %d", got["enriched"], got["not_found"])

    digest = None
    if cmd in ("weekly", "digest"):
        if llm.available:
            try:
                got = run_digest(store, llm, tax)
                digest = got.get("digest")
                log.info("дайджест: %s", "готов" if digest else got.get("note"))
            except Exception as exc:  # noqa: BLE001
                record["errors"].append(f"дайджест: {exc}")
        else:
            record["errors"].append("дайджест пропущен: модель не настроена")

    record["llm"] = dict(llm.usage)
    record["queue"] = len(store.queue)
    store.log_run(record)

    if cmd in ("run", "weekly", "review", "build", "extract", "enrich", "digest", "seed"):
        counts = build(store, tax, settings)
        log.info("страница: компаний %(companies)d, решений %(products)d, событий %(events)d", counts)
    store.save()

    if new_events:
        text = notify.events_message(new_events, settings.notify_min_importance)
        if text:
            notify.send(settings, text)
    if digest:
        notify.send(settings, notify.digest_message(digest))

    for err in record["errors"]:
        log.warning("%s", err)
    if llm.usage["calls"]:
        log.info("модель: вызовов %(calls)d, токенов %(input)d/%(output)d, поисков %(searches)d, "
                 "≈ $%(cost_usd).3f", llm.usage)
    return 0


if __name__ == "__main__":
    sys.exit(main())
