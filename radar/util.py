from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

UTC = timezone.utc


def now() -> datetime:
    return datetime.now(UTC)


def now_iso() -> str:
    return now().strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return now().strftime("%Y-%m-%d")


def sha(text: str, n: int = 12) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def parse_date(value: str | None) -> date | None:
    """'2026-11-16' или ISO-время -> date; мусор -> None."""
    if not value:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(value))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def days_between(a: str | None, b: str | None) -> int | None:
    da, db = parse_date(a), parse_date(b)
    if not da or not db:
        return None
    return abs((da - db).days)


def older_than(iso: str | None, days: int) -> bool:
    d = parse_date(iso)
    return bool(d) and d < (now() - timedelta(days=days)).date()


def strip_html(html: str | None) -> str:
    if not html:
        return ""
    if "<" not in html:
        return re.sub(r"\s+", " ", html).strip()
    text = BeautifulSoup(html, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


_TRACKING = re.compile(r"^(utm_|fbclid|gclid|yclid|ref$|ref_|mc_|igshid|spm$|from$)", re.I)


def canonical_url(url: str) -> str:
    """Убираем метки отслеживания и якоря, чтобы одна статья не считалась дважды."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)]
    host = p.netloc.lower().removeprefix("www.")
    return urlunsplit((p.scheme.lower() or "https", host, p.path.rstrip("/"), urlencode(query), ""))


def norm_title(title: str) -> str:
    t = (title or "").lower().replace("ё", "е")
    t = re.sub(r"[^\w\s]", " ", t, flags=re.U)
    return re.sub(r"\s+", " ", t).strip()


def safe_url(url: str | None) -> str:
    """Пропускаем только http(s)-ссылки: адреса приходят из внешних источников и от модели."""
    u = (url or "").strip()
    return u if re.match(r"^https?://[^\s<>\"']+$", u, re.I) else ""


def clip(text: str | None, n: int) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


# ---------- файлы ----------

def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)             # mkstemp даёт 0600, а страницу читает веб-сервер
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path: Path, default):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, obj, *, compact: bool = False) -> None:
    if compact:
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True)
    write_text_atomic(path, text + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    text = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
    write_text_atomic(path, text)
