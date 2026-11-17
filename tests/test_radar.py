"""Тесты без сети и без ключей: модель подменяется заглушкой.   Запуск:  python -m pytest -q"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from radar.build import build
from radar.collect import KeywordFilter, dedupe_keys, gnews_url, parse_telegram
from radar.config import Settings, Taxonomy, load_yaml
from radar.entities import Registry, load_seed, norm_name, slugify
from radar.extract import build_schema, run_extract
from radar.fx import Rates
from radar.llm import Truncated, parse_json_loose
from radar.store import Store
from radar.util import canonical_url, now, safe_url


class FakeLLM:
    """Отвечает заранее заготовленными событиями по заголовку новости."""
    available = True
    can_search = False

    def __init__(self, answers: dict[str, list[dict]], truncate_over: int = 99) -> None:
        self.answers, self.truncate_over, self.calls = answers, truncate_over, 0
        self.usage = {"calls": 0, "input": 0, "output": 0, "searches": 0, "cost_usd": 0.0}

    def json(self, system, user, schema, **_):
        self.calls += 1
        rows = json.loads(user.split("Новости:\n", 1)[1])
        if len(rows) > self.truncate_over:
            raise Truncated("слишком длинно")
        items = []
        for row in rows:
            events = self.answers.get(row["title"], [])
            items.append({"idx": row["idx"], "relevant": bool(events), "events": events})
        return {"items": items}


def ev(**kw) -> dict:
    base = {"type": "other", "title": "", "summary": "", "date": "", "categories": [], "focus_niches": [],
            "importance": 3, "company_name": "", "company_aliases": [], "company_country": "",
            "company_description": "", "product_name": "", "product_stage": "unknown",
            "product_description": "", "amount": 0, "currency": "", "round_type": "", "investors": [],
            "robots_deployed": 0, "revenue": 0, "revenue_currency": "", "revenue_year": 0, "valuation": 0,
            "valuation_currency": "", "employees": 0, "price": 0, "price_currency": ""}
    return {**base, **kw}


def item(n: int, title: str, source: str = "Test Feed", days_ago: int = 0) -> dict:
    ts = (now() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": f"id{n}", "source": source, "kind": "rss", "region_hint": "", "collected": ts,
            "url": f"https://example.com/news/{n}", "title": title, "text": "text " * 20, "published": ts}


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RADAR_LLM_PROVIDER", "none")
    settings = Settings()
    settings.data_dir, settings.site_dir, settings.batch_size = tmp_path / "data", tmp_path / "site", 4
    store = Store(settings.data_dir)
    rates = Rates({"date": now().strftime("%Y-%m-%d"), "rates": {"USD": 1, "EUR": 0.9, "RUB": 80}})
    return store, Taxonomy(), settings, rates


# ---------- имена компаний ----------

def test_name_normalisation():
    assert norm_name("ООО «Геоскан»") == "геоскан"
    assert norm_name("Gecko Robotics, Inc.") == "gecko robotics"
    assert norm_name("Kärcher GmbH") == "karcher"
    assert slugify("НПО «Андроидная техника»") == "androidnaya-tehnika"


def test_registry_merges_spellings():
    companies: dict = {}
    reg = Registry(companies)
    a = reg.resolve("Gecko Robotics", ["Гекко Роботикс"])
    assert reg.resolve("Gecko Robotics Inc.") == a
    assert reg.resolve("Gecko") == a                   # без слова Robotics
    assert reg.resolve("Гекко Роботикс") == a          # по псевдониму
    assert reg.resolve("Geco Robotics") == a           # опечатка
    assert reg.resolve("Skydio") != a
    assert len(companies) == 2
    assert "Gecko" in companies[a]["aliases"]


def test_seed_is_idempotent_and_valid():
    companies: dict = {}
    seed, tax = load_yaml("seed_companies.yaml"), Taxonomy()
    first = load_seed(companies, seed)
    assert first > 50 and load_seed(companies, seed) == 0
    for c in companies.values():
        assert len(c["country"]) == 2 and c["country"].isupper(), c      # ловит YAML-ловушку NO -> False
        assert set(c["categories"]) <= set(tax.category_ids), c
        assert set(c.get("focus", [])) <= set(tax.focus_ids), c


# ---------- сбор ----------

def test_dedupe_and_urls():
    a = {"url": "https://www.site.com/a/?utm_source=x#top", "title": "Startup raises $5M to build facade robots"}
    b = {"url": "https://site.com/a", "title": "другое"}
    c = {"url": "https://other.com/z", "title": "Startup raises $5M to build facade robots!"}
    assert canonical_url(a["url"]) == canonical_url(b["url"])
    assert dedupe_keys(a)[0] == dedupe_keys(b)[0]            # тот же адрес
    assert dedupe_keys(a)[1] == dedupe_keys(c)[1]            # тот же заголовок в другом издании
    assert safe_url("javascript:alert(1)") == "" and safe_url("https://a.b/c") == "https://a.b/c"
    assert "when%3A3d" in gnews_url("робот", "ru", "3d") and "ceid=RU:ru" in gnews_url("робот", "ru", "3d")


def test_keyword_filter():
    kw = KeywordFilter(load_yaml("sources.yaml")["keywords"])
    ok = lambda t: kw.passes({"title": t, "text": ""})  # noqa: E731
    assert ok("Роботы «Яндекса» начали доставлять лекарства в Москве")
    assert ok("Drone maker raises $40M Series B")
    assert not ok("В Азове при атаке БПЛА пострадали три человека")
    assert not ok("Центробанк сохранил ключевую ставку")


def test_parse_telegram():
    html = """
    <div class="tgme_channel_info_header_title"><span>Канал про роботов</span></div>
    <div class="tgme_widget_message" data-post="robochan/101">
      <div class="tgme_widget_message_text">Компания X привлекла $7 млн<br/>Калифорнийский стартап X,
      который делает роботов для лесозаготовки, закрыл посевной раунд на 7 миллионов долларов.</div>
      <a class="tgme_widget_message_date" href="#"><time datetime="2026-11-13T13:40:33+00:00"></time></a>
    </div>
    <div class="tgme_widget_message" data-post="robochan/102">
      <div class="tgme_widget_message_text">Коротко</div>
    </div>"""
    items = parse_telegram(html, "robochan", "ru")
    assert len(items) == 1                                   # короткий пост отброшен
    it = items[0]
    assert it["url"] == "https://t.me/robochan/101" and it["source"] == "Telegram: Канал про роботов"
    assert it["title"] == "Компания X привлекла $7 млн" and it["published"] == "2026-11-13T13:40:33Z"


# ---------- разбор ----------

def test_schema_fits_structured_output_limits():
    """Structured outputs: у каждого объекта additionalProperties=false, без union-типов и
    необязательных полей (лимиты API: 16 union-параметров, 24 необязательных)."""
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert sorted(node["required"]) == sorted(node["properties"])
            assert not isinstance(node.get("type"), list) and "anyOf" not in node
            for bad in ("minimum", "maximum", "minLength", "maxLength"):
                assert bad not in node
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(build_schema(Taxonomy()))


def test_extract_end_to_end(env):
    store, tax, settings, rates = env
    load_seed(store.companies, load_yaml("seed_companies.yaml"))
    seeded = len(store.companies)
    funding = ev(type="funding", title="VertiDrive привлекла €9 млн", summary="Раунд серии A.",
                 categories=["inspection"], focus_niches=["tank"], importance=4,
                 company_name="VertiDrive B.V.", company_country="NL", amount=9_000_000, currency="EUR",
                 round_type="series_a", investors=["Fund One"], robots_deployed=120)
    store.queue = [
        item(1, "VertiDrive raises EUR 9M"),
        item(2, "VertiDrive closes Series A", source="Other Feed"),       # та же новость в другом издании
        item(3, "Погода на завтра"),
        item(4, "Новый робот от стартапа"),
        item(5, "Старая новость", days_ago=30),
    ]
    llm = FakeLLM({
        "VertiDrive raises EUR 9M": [funding],
        "VertiDrive closes Series A": [dict(funding, title="VertiDrive закрыла раунд A", investors=["Fund Two"])],
        "Новый робот от стартапа": [ev(type="product", title="НоваРобот показал мойщика фасадов",
                                       categories=["cleaning", "ERROR"], focus_niches=["facade"],
                                       company_name="НоваРобот", company_country="ru", product_name="Фасад-1",
                                       product_stage="prototype", price=3_200_000, price_currency="RUB",
                                       date="2031-01-01")],
    })
    stats = run_extract(store, llm, tax, settings, rates)

    assert stats["dropped_old"] == 1 and stats["processed"] == 4 and stats["relevant"] == 3
    assert stats["events_new"] == 2 and stats["events_merged"] == 1 and stats["companies_new"] == 1
    assert store.queue == [] and len(store.companies) == seeded + 1

    deal = next(e for e in store.events if e["type"] == "funding")
    assert deal["company_id"] == "vertidrive" and deal["country"] == "NL"
    assert deal["amount"] == {"value": 9_000_000, "currency": "EUR", "usd": 10_000_000.0}
    assert len(deal["sources"]) == 2 and set(deal["investors"]) == {"Fund One", "Fund Two"}
    card = store.companies["vertidrive"]
    assert card["last_round"]["usd"] == 10_000_000.0 and card["robots_deployed"]["value"] == 120
    assert card["origin"] == "seed" and card["aliases"] == []     # «B.V.» — не новое имя

    launch = next(e for e in store.events if e["type"] == "product")
    assert launch["categories"] == ["cleaning"] and launch["country"] == "RU"
    assert launch["date"] == store.events[0]["date"][:10] or launch["date"] <= now().strftime("%Y-%m-%d")
    product = store.products[launch["product_id"]]
    assert product["stage"] == "prototype" and product["price"]["usd"] == 40_000.0

    store.save()
    counts = build(store, tax, settings)
    page = json.loads((settings.site_dir / "data" / "radar.json").read_text(encoding="utf-8"))
    assert counts["events"] == 2 and page["meta"]["counts"]["companies"] == seeded + 1
    vd = next(c for c in page["companies"] if c["id"] == "vertidrive")
    assert vd["events"] == 1 and vd["funding_seen_usd"] == 10_000_000.0
    assert Store(settings.data_dir).events == store.events          # файлы читаются обратно без потерь


def test_truncated_answer_splits_batch(env):
    store, tax, settings, rates = env
    store.queue = [item(i, f"Новость {i}") for i in range(4)]
    llm = FakeLLM({"Новость 2": [ev(type="pilot", title="Пилот", company_name="Acme Robots")]}, truncate_over=2)
    stats = run_extract(store, llm, tax, settings, rates)
    assert stats["processed"] == 4 and stats["events_new"] == 1 and llm.calls == 3


def test_queue_waits_without_model(env, monkeypatch):
    store, tax, settings, rates = env
    from radar.llm import LLM
    store.queue = [item(1, "Новость")]
    stats = run_extract(store, LLM(settings), tax, settings, rates)
    assert stats["left_in_queue"] == 1 and stats["errors"]


def test_parse_json_loose():
    assert parse_json_loose('Вот ответ:\n```json\n{"items": []}\n```') == {"items": []}
    assert parse_json_loose('{"a": 1}') == {"a": 1}


# ---------- запросы к Claude API (на подставном клиенте, без сети) ----------

def _reply(text="", stop="end_turn", blocks=None, searches=0):
    from types import SimpleNamespace as NS
    usage = NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0,
               server_tool_use=NS(web_search_requests=searches))
    return NS(content=blocks or [NS(type="text", text=text)], stop_reason=stop, usage=usage)


def _claude(monkeypatch, replies):
    import anthropic
    import httpx
    from radar.llm import LLM
    monkeypatch.setenv("RADAR_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    llm, calls = LLM(Settings()), []

    def create(**kwargs):
        calls.append(kwargs)
        reply = replies.pop(0)
        if reply == "400":
            raise anthropic.BadRequestError("bad", body=None, response=httpx.Response(
                400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")))
        return reply

    monkeypatch.setattr(llm._client.messages, "create", create)
    return llm, calls


def test_claude_json_request_shape(monkeypatch):
    llm, calls = _claude(monkeypatch, [_reply('{"items": []}')])
    schema = build_schema(Taxonomy())
    assert llm.json("система", "новости", schema) == {"items": []}
    sent = calls[0]
    assert sent["model"] == "claude-haiku-5-5" and sent["max_tokens"] <= 21000     # без стриминга
    assert sent["output_config"] == {"format": {"type": "json_schema", "schema": schema}}
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    # на моделях 5.5 это вернуло бы 400: принудительный вызов инструмента, температура, префилл
    assert not {"tool_choice", "temperature", "top_p", "thinking"} & set(sent)
    assert sent["messages"] == [{"role": "user", "content": "новости"}]
    assert llm.usage["calls"] == 1 and llm.usage["cost_usd"] > 0


def test_claude_falls_back_when_structured_outputs_rejected(monkeypatch):
    llm, calls = _claude(monkeypatch, ["400", _reply('```json\n{"items": []}\n```')])
    assert llm.json("система", "новости", {"type": "object"}) == {"items": []}
    assert "output_config" not in calls[1] and "JSON Schema" in calls[1]["messages"][0]["content"]


def test_claude_truncated_and_research_pause(monkeypatch):
    from types import SimpleNamespace as NS
    llm, _ = _claude(monkeypatch, [_reply("{", stop="max_tokens")])
    with pytest.raises(Truncated):
        llm.json("s", "u", {"type": "object"})

    found = NS(type="web_search_tool_result", content=[NS(url="https://a.example/1"), NS(url="https://a.example/1")])
    llm, calls = _claude(monkeypatch, [
        _reply(stop="pause_turn", blocks=[NS(type="text", text="Часть 1. "), found], searches=2),
        _reply("Часть 2.", searches=1)])
    text, urls = llm.research("система", "компания")
    assert text == "Часть 1. Часть 2." and urls == ["https://a.example/1"]
    assert calls[0]["model"] == "claude-sonnet-5-5"
    assert calls[0]["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    assert calls[1]["messages"][1]["role"] == "assistant" and llm.usage["searches"] == 3


def test_enrich_profile_needs_sources(env):
    from radar.enrich import apply_profile, profile_schema
    store, tax, settings, rates = env
    card = store.companies["acme"] = {"id": "acme", "name": "Acme", "aliases": [], "categories": ["cleaning"], "focus": []}
    blank = {k: ([] if v.get("type") == "array" else "" if v.get("type") == "string" else 0)
             for k, v in profile_schema(tax)["properties"].items()}
    apply_profile(store, tax, rates, card, {**blank, "found": True, "website": "https://acme.example",
                  "country": "de", "status": "active", "founded_year": 2019,
                  "revenue": 4_000_000, "revenue_currency": "EUR", "revenue_year": 2025, "revenue_source": "",
                  "funding_total": 9_000_000, "funding_total_currency": "EUR",
                  "funding_total_source": "https://news.example/acme",
                  "products": [{"name": "Scrub-1", "stage": "serial", "description": "Поломоечный робот",
                                "price": 0, "price_currency": "", "source": "javascript:alert(1)"}]})
    assert card["website"] == "https://acme.example" and card["country"] == "DE" and card["founded"] == 2019
    assert card["funding_total"]["usd"] == 10_000_000.0
    assert "revenue" not in card                       # цифра без источника не записывается
    assert store.products["acme--scrub-1"]["stage"] == "serial" and store.products["acme--scrub-1"]["sources"] == []


def test_collect_retries_rate_limited_search(monkeypatch):
    """Google News на частые запросы отвечает 429: идём по одному, потом второй заход по отказам."""
    import radar.collect as c
    calls, sleeps = [], []
    def fake(client, kind, src, window):
        calls.append(src.get("q") or src.get("name"))
        if kind == "gnews" and calls.count(src["q"]) == 1 and src["q"] == "b":
            return [], "HTTP 429"
        return [], ""
    monkeypatch.setattr(c, "_fetch_source", fake)
    monkeypatch.setattr(c.time, "sleep", sleeps.append)
    got = c.collect(None, {"rss": [{"name": "r", "url": "x"}],
                           "google_news": [{"q": "a"}, {"q": "b"}, {"q": "c"}]}, dry_run=True)
    assert calls == ["r", "a", "b", "c", "b"] and not got["errors"]       # второй заход — только по отказу
    assert sleeps == [c.GENTLE_PAUSE] * 3 + [c.RETRY_AFTER, c.GENTLE_PAUSE * 1.5]
