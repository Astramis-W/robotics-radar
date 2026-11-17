"""Обёртка над моделью. По умолчанию Claude API; можно подключить любую модель
с OpenAI-совместимым API (переменные OPENAI_COMPAT_*), тогда обогащение через веб-поиск
отключается, а разбор новостей и дайджест работают."""
from __future__ import annotations

import json
import logging
import re

import httpx

from .config import Settings

log = logging.getLogger("radar.llm")

# $ за 1 млн токенов (вход, выход) — для оценки расхода в журнале прогонов.
# Сверено 16.11.2026: https://platform.claude.com/docs/en/about-claude/pricing
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
WEB_SEARCH_PRICE = 10.0 / 1000      # $ за один поиск


class LLMError(RuntimeError):
    pass


class Truncated(LLMError):
    """Ответ упёрся в max_tokens — вызывающему стоит уменьшить пачку."""


def parse_json_loose(text: str) -> dict:
    """JSON из ответа модели, даже если вокруг него есть текст или ```-ограждение."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        return json.loads(fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise LLMError("модель вернула не JSON")


class LLM:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.provider = settings.provider
        self.structured = True        # structured outputs; выключается сам, если API их отклонит
        self.usage = {"calls": 0, "input": 0, "output": 0, "searches": 0, "cost_usd": 0.0}
        self._client = None
        if self.provider == "anthropic":
            import anthropic
            self._anthropic = anthropic
            self._client = anthropic.Anthropic(max_retries=4, timeout=300)

    @property
    def available(self) -> bool:
        return self.provider in ("anthropic", "openai")

    @property
    def can_search(self) -> bool:
        return self.provider == "anthropic"

    # ---------- учёт ----------

    def _count(self, model: str, inp: int, out: int, searches: int = 0) -> None:
        pin, pout = PRICES.get(model, (0.0, 0.0))
        self.usage["calls"] += 1
        self.usage["input"] += inp
        self.usage["output"] += out
        self.usage["searches"] += searches
        self.usage["cost_usd"] = round(
            self.usage["cost_usd"] + inp / 1e6 * pin + out / 1e6 * pout + searches * WEB_SEARCH_PRICE, 6)

    # ---------- Claude ----------

    def _claude(self, model: str, system: str, messages: list, max_tokens: int,
                schema: dict | None = None, tools: list | None = None):
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }
        output_config: dict = {}
        if schema is not None and self.structured:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if self.s.effort:
            output_config["effort"] = self.s.effort
        if output_config:
            kwargs["output_config"] = output_config
        if tools:
            kwargs["tools"] = tools
        resp = self._client.messages.create(**kwargs)
        u = resp.usage
        inp = (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) \
            + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        searches = 0
        stu = getattr(u, "server_tool_use", None)
        if stu is not None:
            searches = getattr(stu, "web_search_requests", 0) or 0
        self._count(model, inp, u.output_tokens or 0, searches)
        return resp

    @staticmethod
    def _text_of(resp) -> str:
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    # ---------- OpenAI-совместимый API ----------

    def _openai(self, system: str, user: str, max_tokens: int) -> str:
        r = httpx.post(
            self.s.openai_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {self.s.openai_api_key or ''}"},
            json={"model": self.s.openai_model, "max_tokens": max_tokens, "temperature": 0,
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            timeout=300,
        )
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage") or {}
        self._count(self.s.openai_model or "", usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        return data["choices"][0]["message"]["content"] or ""

    # ---------- публичные методы ----------

    @staticmethod
    def _schema_hint(schema: dict) -> str:
        return ("\n\nВерни только один JSON-объект без пояснений и без ```, строго по этой JSON Schema:\n"
                + json.dumps(schema, ensure_ascii=False))

    def json(self, system: str, user: str, schema: dict, *, smart: bool = False,
             max_tokens: int = 16000) -> dict:
        """Ответ модели как JSON по схеме."""
        if not self.available:
            raise LLMError("модель не настроена: задайте ANTHROPIC_API_KEY или OPENAI_COMPAT_*")
        if self.provider == "openai":
            return parse_json_loose(self._openai(system, user + self._schema_hint(schema), max_tokens))

        model = self.s.model_smart if smart else self.s.model_fast
        content = user if self.structured else user + self._schema_hint(schema)
        try:
            resp = self._claude(model, system, [{"role": "user", "content": content}], max_tokens, schema)
        except self._anthropic.BadRequestError as exc:
            if not self.structured:
                raise LLMError(f"API отклонил запрос: {exc}") from exc
            log.warning("structured outputs не приняты (%s) — перехожу на JSON в тексте", exc)
            self.structured = False
            return self.json(system, user, schema, smart=smart, max_tokens=max_tokens)
        if resp.stop_reason == "max_tokens":
            raise Truncated("ответ обрезан по max_tokens")
        if resp.stop_reason == "refusal":
            raise LLMError("модель отказалась отвечать на этот запрос")
        return parse_json_loose(self._text_of(resp))

    def text(self, system: str, user: str, *, smart: bool = True, max_tokens: int = 6000) -> str:
        if not self.available:
            raise LLMError("модель не настроена")
        if self.provider == "openai":
            return self._openai(system, user, max_tokens)
        model = self.s.model_smart if smart else self.s.model_fast
        return self._text_of(self._claude(model, system, [{"role": "user", "content": user}], max_tokens))

    def research(self, system: str, user: str, *, max_tokens: int = 6000) -> tuple[str, list[str]]:
        """Ответ с веб-поиском (серверный инструмент Claude). Возвращает текст и найденные адреса."""
        if not self.can_search:
            raise LLMError("веб-поиск доступен только с Claude API")
        tools = [{"type": self.s.web_search_tool, "name": "web_search",
                  "max_uses": self.s.web_search_max_uses}]
        messages: list = [{"role": "user", "content": user}]
        chunks, urls = [], []
        for _ in range(4):
            resp = self._claude(self.s.model_smart, system, messages, max_tokens, tools=tools)
            chunks.append(self._text_of(resp))
            for block in resp.content:
                if getattr(block, "type", "") == "web_search_tool_result" and isinstance(block.content, list):
                    urls.extend(getattr(r, "url", "") for r in block.content)
            if resp.stop_reason != "pause_turn":      # длинный поиск: API просит продолжить ход
                break
            messages.append({"role": "assistant", "content": resp.content})
        return "".join(chunks).strip(), [u for u in dict.fromkeys(urls) if u]
