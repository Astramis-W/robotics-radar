"""Сбор сырья: RSS-ленты, поиск Google News, публичные Telegram-каналы."""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import quote_plus

import feedparser
import httpx
from bs4 import BeautifulSoup

from .store import Store
from .util import UTC, canonical_url, clip, norm_title, now, now_iso, sha, strip_html

log = logging.getLogger("radar.collect")

UA = "Mozilla/5.0 (compatible; RoboticsRadar/1.0)"
TEXT_LIMIT = 6000
GENTLE_PAUSE = 2.0      # секунд между запросами к Google News и Telegram
RETRY_AFTER = 10.0      # пауза перед вторым заходом по тем, кто ответил 429/503
THROTTLED = ("HTTP 429", "HTTP 503")


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"},
        timeout=httpx.Timeout(25, connect=10),
        follow_redirects=True,
    )


def _item(source: str, kind: str, url: str, title: str, text: str, published: datetime | None,
          region: str | None) -> dict:
    return {
        "id": sha(canonical_url(url)),
        "source": source,
        "kind": kind,                      # rss | gnews | telegram
        "region_hint": region or "",
        "url": url,
        "title": clip(title, 300),
        "text": (text or "")[:TEXT_LIMIT],
        "published": (published or now()).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "collected": now_iso(),
    }


def _entry_time(e) -> datetime | None:
    t = e.get("published_parsed") or e.get("updated_parsed")
    if not t:
        return None
    try:
        return datetime(*t[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


# ---------- RSS ----------

def fetch_rss(client: httpx.Client, src: dict) -> list[dict]:
    r = client.get(src["url"])
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    if not feed.entries:
        raise ValueError("в ленте нет записей (изменился адрес или формат?)")
    items = []
    for e in feed.entries:
        url, title = e.get("link"), strip_html(e.get("title"))
        if not url or not title:
            continue
        body = ""
        if e.get("content"):
            body = e["content"][0].get("value", "")
        body = strip_html(body or e.get("summary") or "")
        items.append(_item(src["name"], "rss", url, title, body, _entry_time(e), src.get("region")))
    return items


# ---------- Google News ----------

def gnews_url(query: str, lang: str, window: str) -> str:
    q = quote_plus(f"{query} when:{window}")
    if lang == "ru":
        return f"https://news.google.com/rss/search?q={q}&hl=ru&gl=RU&ceid=RU:ru"
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def fetch_gnews(client: httpx.Client, src: dict, window: str) -> list[dict]:
    r = client.get(gnews_url(src["q"], src.get("lang", "en"), window))
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    items = []
    for e in feed.entries:
        title = strip_html(e.get("title"))
        publisher = (e.get("source") or {}).get("title") or ""
        if publisher and title.endswith(" - " + publisher):
            title = title[: -len(publisher) - 3]
        if not e.get("link") or not title:
            continue
        # В выдаче только заголовок; сама статья — по ссылке-редиректу Google.
        items.append(_item(publisher or "Google News", "gnews", e["link"], title, "",
                           _entry_time(e), "ru" if src.get("lang") == "ru" else None))
    return items


# ---------- Telegram (публичное веб-превью) ----------

def parse_telegram(html: str, channel: str, region: str | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    name_el = soup.select_one(".tgme_channel_info_header_title")
    name = name_el.get_text(strip=True) if name_el else channel
    items = []
    for msg in soup.select("div.tgme_widget_message"):
        post = msg.get("data-post")
        text_el = msg.select_one(".tgme_widget_message_text")
        if not post or not text_el:
            continue
        for br in text_el.find_all("br"):
            br.replace_with("\n")
        raw = text_el.get_text()
        lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw.split("\n")]
        lines = [ln for ln in lines if ln]
        text = " ".join(lines)
        if len(text) < 80:               # подписи к картинкам, опросы и т.п.
            continue
        time_el = msg.select_one("a.tgme_widget_message_date time")
        published = None
        if time_el and time_el.get("datetime"):
            try:
                published = datetime.fromisoformat(time_el["datetime"])
            except ValueError:
                published = None
        items.append(_item(f"Telegram: {name}", "telegram", f"https://t.me/{post}",
                           lines[0], text, published, region))
    return items


def fetch_telegram(client: httpx.Client, src: dict) -> list[dict]:
    channel = src["channel"].lstrip("@")
    r = client.get(f"https://t.me/s/{channel}")
    r.raise_for_status()
    items = parse_telegram(r.text, channel, src.get("region"))
    if not items and "tgme_widget_message" not in r.text:
        raise ValueError("нет публичного превью (канал закрытый, пустой или переименован)")
    return items


# ---------- фильтры ----------

class KeywordFilter:
    def __init__(self, keywords: dict | None) -> None:
        kw = keywords or {}
        inc, exc = kw.get("include") or [], kw.get("exclude") or []
        self.include = re.compile("|".join(f"(?:{p})" for p in inc), re.I) if inc else None
        self.exclude = re.compile("|".join(f"(?:{p})" for p in exc), re.I) if exc else None

    def passes(self, item: dict) -> bool:
        blob = f"{item['title']} {item['text']}"
        if self.include and not self.include.search(blob):
            return False
        if self.exclude and self.exclude.search(item["title"]):
            return False
        return True


def dedupe_keys(item: dict) -> list[str]:
    keys = ["u:" + sha(canonical_url(item["url"]))]
    nt = norm_title(item["title"])
    if len(nt) >= 30:
        keys.append("t:" + sha(nt))
    return keys


def fetch_fulltext(client: httpx.Client, url: str) -> str:
    """Дочитываем статью, когда в ленте только анонс. Нужен пакет trafilatura (необязательный)."""
    try:
        import trafilatura
    except ImportError:
        return ""
    try:
        r = client.get(url, timeout=15)
        r.raise_for_status()
        return (trafilatura.extract(r.text, include_comments=False, include_tables=False) or "")[:TEXT_LIMIT]
    except Exception as exc:  # noqa: BLE001 — сеть: любая ошибка просто оставляет анонс
        log.debug("fulltext %s: %s", url, exc)
        return ""


# ---------- основной проход ----------

def _fetch_source(client: httpx.Client, kind: str, src: dict, window: str) -> tuple[list[dict], str]:
    try:
        if kind == "rss":
            return fetch_rss(client, src), ""
        if kind == "gnews":
            return fetch_gnews(client, src, window), ""
        return fetch_telegram(client, src), ""
    except httpx.HTTPStatusError as exc:
        return [], f"HTTP {exc.response.status_code}"
    except Exception as exc:  # noqa: BLE001 — один упавший источник не должен ронять прогон
        return [], clip(f"{type(exc).__name__}: {exc}", 160)


def _fetch_gently(client: httpx.Client, jobs: list[tuple[str, dict]], window: str) -> list[tuple[list[dict], str]]:
    """Google News и Telegram: по одному запросу с паузой, затем второй заход по отказам.

    На частые запросы с одного адреса Google News отвечает 429 — выборочно, примерно на каждый
    четвёртый даже при паузе в две секунды. Второй заход добирает почти всё; что не добралось,
    попадёт в следующий прогон (окно поиска — несколько дней, новости не теряются)."""
    results = {}
    for i, (kind, src) in enumerate(jobs):
        results[i] = _fetch_source(client, kind, src, window)
        time.sleep(GENTLE_PAUSE)
    again = [i for i, got in results.items() if got[1] in THROTTLED]
    if again:
        time.sleep(RETRY_AFTER)
        for i in again:
            results[i] = _fetch_source(client, jobs[i][0], jobs[i][1], window)
            time.sleep(GENTLE_PAUSE * 1.5)
    return [results[i] for i in range(len(jobs))]


def source_label(kind: str, src: dict) -> str:
    if kind == "rss":
        return src["name"]
    if kind == "gnews":
        return f"Google News: {src['q']}"
    return f"Telegram: @{src['channel'].lstrip('@')}"


def collect(store: Store | None, sources: dict, *, dry_run: bool = False) -> dict:
    """Собирает новые записи в очередь. Возвращает статистику по источникам."""
    settings = sources.get("settings") or {}
    max_age = int(settings.get("max_age_days", 7))
    window = str(settings.get("gnews_window", "3d"))
    fulltext_limit = int(settings.get("fulltext_limit", 25))
    cutoff = now() - timedelta(days=max_age)
    kw = KeywordFilter(sources.get("keywords"))

    jobs = [("rss", s) for s in sources.get("rss") or []]
    slow_jobs = [("gnews", s) for s in sources.get("google_news") or []]
    slow_jobs += [("telegram", s) for s in sources.get("telegram") or []]

    stats, fresh = [], []
    seen = dict(store.seen) if store else {}
    queued = {q["id"] for q in store.queue} if store else set()

    with make_client() as client:
        with ThreadPoolExecutor(max_workers=8) as pool:      # RSS — разные сайты, можно разом
            results = list(pool.map(lambda j: _fetch_source(client, j[0], j[1], window), jobs))
        results += _fetch_gently(client, slow_jobs, window)

        for (kind, src), (items, error) in zip(jobs + slow_jobs, results):
            new = 0
            for it in items:
                if datetime.strptime(it["published"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) < cutoff:
                    continue
                keys = dedupe_keys(it)
                if any(k in seen for k in keys) or it["id"] in queued:
                    continue
                for k in keys:
                    seen[k] = it["published"][:10]
                if src.get("filter") and not kw.passes(it):
                    continue
                if kind != "rss" and kw.exclude and kw.exclude.search(it["title"]):
                    continue                      # военная хроника из поиска и каналов
                fresh.append(it)
                queued.add(it["id"])
                new += 1
            stats.append({"source": source_label(kind, src), "kind": kind, "fetched": len(items),
                          "new": new, "error": error})
            if error:
                log.warning("%s: %s", source_label(kind, src), error)

        if not dry_run:
            short = [it for it in fresh if it["kind"] == "rss" and len(it["text"]) < 400]
            for it in short[:fulltext_limit]:
                full = fetch_fulltext(client, it["url"])
                if len(full) > len(it["text"]):
                    it["text"] = full

    if store and not dry_run:
        store.seen = seen
        store.queue.extend(fresh)
    return {"sources": stats, "new_items": len(fresh),
            "errors": [f"{s['source']}: {s['error']}" for s in stats if s["error"]]}
