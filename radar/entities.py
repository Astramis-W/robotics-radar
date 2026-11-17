"""Склейка упоминаний компаний: «Gecko Robotics, Inc.», «Gecko» и «Гекко Роботикс» — одна карточка."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from .util import today

_LEGAL = (
    r"inc|incorporated|llc|ltd|limited|corp|corporation|co|company|gmbh|ag|sa|sas|"
    r"bv|nv|oy|oyj|asa|aps|plc|pte|pty|kk|srl|spa|holdings?|group|"
    r"ооо|ао|пао|зао|оао|гк|нпо|нпп|нпк|ип|группа компаний|компания"
)
_LEGAL_RE = re.compile(rf"(?<!\w)(?:{_LEGAL})(?!\w)", re.I | re.U)
_GENERIC_RE = re.compile(r"\b(robotics|robots|роботикс|робототехника)\b", re.I | re.U)

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
_LATIN_FOLD = str.maketrans("äöüàáâãåçèéêëìíîïñòóôõùúûýÿøæœß", "aouaaaaaceeeeiiiinoooouuuyyoaos")


def norm_name(name: str) -> str:
    """Ключ сравнения: без регистра, кавычек, организационно-правовых форм и пунктуации."""
    s = (name or "").lower().replace("ё", "е").translate(_LATIN_FOLD)
    s = re.sub(r"[«»\"'“”„`’]", " ", s)
    s = re.sub(r"(?<=\b\w)\.(?=\w\b)", "", s)          # B.V. -> BV, S.r.l. -> Srl
    s = re.sub(r"[^\w\s+&-]", " ", s, flags=re.U)
    s = _LEGAL_RE.sub(" ", s)
    s = re.sub(r"[-_]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def core_name(name: str) -> str:
    """Имя без слов-пустышек вроде Robotics — ловит «Gecko» против «Gecko Robotics»."""
    core = re.sub(r"\s+", " ", _GENERIC_RE.sub(" ", norm_name(name))).strip()
    return core if len(core) >= 4 else ""


def slugify(name: str) -> str:
    s = norm_name(name)
    s = "".join(_TRANSLIT.get(ch, ch) for ch in s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "company"


class Registry:
    """Индекс имён и псевдонимов поверх словаря компаний из Store."""

    def __init__(self, companies: dict[str, dict]) -> None:
        self.companies = companies
        self.by_name: dict[str, str] = {}
        self.by_core: dict[str, str | None] = {}     # None — ядро неоднозначно, по нему не клеим
        for cid, c in companies.items():
            self._index(cid, [c.get("name", "")] + list(c.get("aliases") or []))

    def _index(self, cid: str, names: list[str]) -> None:
        for n in names:
            key = norm_name(n)
            if key:
                self.by_name.setdefault(key, cid)
            core = core_name(n)
            if core:
                if core in self.by_core and self.by_core[core] != cid:
                    self.by_core[core] = None
                else:
                    self.by_core[core] = cid

    def find(self, name: str, aliases: list[str] | None = None) -> str | None:
        names = [name] + list(aliases or [])
        for n in names:
            cid = self.by_name.get(norm_name(n))
            if cid:
                return cid
        core = core_name(name)
        if core and self.by_core.get(core):
            return self.by_core[core]
        key = norm_name(name)
        if len(key) >= 7:                             # опечатки и варианты написания
            best, best_ratio = None, 0.0
            for known, cid in self.by_name.items():
                if abs(len(known) - len(key)) > 3:
                    continue
                ratio = SequenceMatcher(None, key, known).ratio()
                if ratio > best_ratio:
                    best, best_ratio = cid, ratio
            if best and best_ratio >= 0.93:
                return best
        return None

    def resolve(self, name: str, aliases: list[str] | None = None, **fields) -> str | None:
        """Находит компанию или заводит новую карточку. Возвращает id."""
        name = re.sub(r"\s+", " ", name or "").strip()
        if len(norm_name(name)) < 2:
            return None
        aliases = [a.strip() for a in (aliases or []) if a and a.strip()]
        cid = self.find(name, aliases)
        if cid:
            card = self.companies[cid]
            known = {norm_name(card["name"])} | {norm_name(a) for a in card.get("aliases", [])}
            for a in [name] + aliases:
                if norm_name(a) and norm_name(a) not in known:
                    card.setdefault("aliases", []).append(a)
                    known.add(norm_name(a))
            self._index(cid, [name] + aliases)
            return cid

        base = slugify(name)
        cid, n = base, 2
        while cid in self.companies:
            cid, n = f"{base}-{n}", n + 1
        self.companies[cid] = {
            "id": cid,
            "name": name,
            "aliases": aliases,
            "country": "",
            "categories": [],
            "focus": [],
            "description": "",
            "origin": "news",
            "first_seen": today(),
            "updated": today(),
            **{k: v for k, v in fields.items() if v not in (None, "", [])},
        }
        self._index(cid, [name] + aliases)
        return cid


def load_seed(companies: dict[str, dict], seed: dict) -> int:
    """Добавляет компании из config/seed_companies.yaml, которых ещё нет в базе."""
    reg = Registry(companies)
    added = 0
    for row in seed.get("companies") or []:
        if reg.find(row["name"], row.get("aliases")):
            continue
        reg.resolve(
            row["name"], row.get("aliases"),
            country=str(row.get("country") or ""),
            categories=list(row.get("categories") or []),
            focus=list(row.get("focus") or []),
            origin="seed",
        )
        added += 1
    return added
