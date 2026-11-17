from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def load_yaml(name: str) -> dict:
    path = ROOT / "config" / name
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@dataclass
class Settings:
    """Настройки из переменных окружения (в GitHub Actions — secrets и variables)."""

    data_dir: Path = field(default_factory=lambda: ROOT / (env("RADAR_DATA_DIR", "data") or "data"))
    site_dir: Path = field(default_factory=lambda: ROOT / (env("RADAR_SITE_DIR", "site") or "site"))

    # --- модель ---
    provider: str = ""            # anthropic | openai | none (определяется автоматически)
    model_fast: str = ""          # разбор новостей
    model_smart: str = ""         # обогащение карточек и дайджест
    effort: str | None = None     # low | medium | high; пусто — по умолчанию модели
    web_search_tool: str = ""
    web_search_max_uses: int = 5
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    openai_model: str | None = None

    # --- объёмы ---
    batch_size: int = 8
    max_items_per_run: int = 400
    enrich_limit: int = 12
    enrich_refresh_days: int = 90

    # --- уведомления ---
    tg_token: str | None = None
    tg_chat: str | None = None
    notify_min_importance: int = 4

    def __post_init__(self) -> None:
        self.model_fast = env("RADAR_MODEL_FAST", "claude-haiku-5-5") or ""
        self.model_smart = env("RADAR_MODEL_SMART", "claude-sonnet-5-5") or ""
        self.effort = env("RADAR_EFFORT")
        self.web_search_tool = env("RADAR_WEB_SEARCH_TOOL", "web_search_20250305") or ""
        self.web_search_max_uses = int(env("RADAR_WEB_SEARCH_MAX_USES", "5") or 5)
        self.openai_base_url = env("OPENAI_COMPAT_BASE_URL")
        self.openai_api_key = env("OPENAI_COMPAT_API_KEY")
        self.openai_model = env("OPENAI_COMPAT_MODEL")
        self.batch_size = int(env("RADAR_BATCH_SIZE", "8") or 8)
        self.max_items_per_run = int(env("RADAR_MAX_ITEMS_PER_RUN", "400") or 400)
        self.enrich_limit = int(env("RADAR_ENRICH_LIMIT", "12") or 12)
        self.enrich_refresh_days = int(env("RADAR_ENRICH_REFRESH_DAYS", "90") or 90)
        self.tg_token = env("TELEGRAM_BOT_TOKEN")
        self.tg_chat = env("TELEGRAM_CHAT_ID")
        self.notify_min_importance = int(env("RADAR_NOTIFY_MIN_IMPORTANCE", "4") or 4)

        provider = (env("RADAR_LLM_PROVIDER") or "").lower()
        if not provider:
            if env("ANTHROPIC_API_KEY"):
                provider = "anthropic"
            elif self.openai_base_url and self.openai_model:
                provider = "openai"
            else:
                provider = "none"
        self.provider = provider


class Taxonomy:
    def __init__(self, raw: dict | None = None) -> None:
        raw = raw if raw is not None else load_yaml("taxonomy.yaml")
        self.categories: list[dict] = raw.get("categories", [])
        self.event_types: list[dict] = raw.get("event_types", [])
        self.stages: list[dict] = raw.get("stages", [])
        self.focus: list[dict] = raw.get("focus_niches", [])

    @staticmethod
    def _ids(rows: list[dict]) -> list[str]:
        return [r["id"] for r in rows]

    @property
    def category_ids(self) -> list[str]:
        return self._ids(self.categories)

    @property
    def event_type_ids(self) -> list[str]:
        return self._ids(self.event_types)

    @property
    def stage_ids(self) -> list[str]:
        return self._ids(self.stages)

    @property
    def focus_ids(self) -> list[str]:
        return self._ids(self.focus)

    def public(self) -> dict:
        strip = lambda rows: [{"id": r["id"], "label": r["label"]} for r in rows]  # noqa: E731
        return {
            "categories": strip(self.categories),
            "event_types": strip(self.event_types),
            "stages": strip(self.stages),
            "focus": strip(self.focus),
        }
