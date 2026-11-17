from __future__ import annotations

from pathlib import Path

from .util import older_than, read_json, read_jsonl, write_json, write_jsonl

SEEN_TTL_DAYS = 21     # дольше хранить незачем: новости старше недели и так отсекаются по дате
RUNS_KEEP = 200


class Store:
    """База радара — текстовые файлы в data/. Их удобно хранить в git: видно историю,
    можно поправить руками, не раздувается репозиторий (в отличие от бинарной SQLite)."""

    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.companies: dict[str, dict] = read_json(self.dir / "companies.json", {})
        self.products: dict[str, dict] = read_json(self.dir / "products.json", {})
        self.events: list[dict] = read_jsonl(self.dir / "events.jsonl")
        self.queue: list[dict] = read_jsonl(self.dir / "queue.jsonl")
        self.seen: dict[str, str] = read_json(self.dir / "seen.json", {})
        self.digests: list[dict] = read_json(self.dir / "digests.json", [])
        self.runs: list[dict] = read_jsonl(self.dir / "runs.jsonl")
        self.fx: dict = read_json(self.dir / "fx.json", {})

    def save(self) -> None:
        self.seen = {k: v for k, v in self.seen.items() if not older_than(v, SEEN_TTL_DAYS)}
        self.events.sort(key=lambda e: (e.get("date") or "", e.get("id") or ""))
        write_json(self.dir / "companies.json", self.companies)
        write_json(self.dir / "products.json", self.products)
        write_jsonl(self.dir / "events.jsonl", self.events)
        write_jsonl(self.dir / "queue.jsonl", self.queue)
        write_json(self.dir / "seen.json", self.seen)
        write_json(self.dir / "digests.json", self.digests)
        write_jsonl(self.dir / "runs.jsonl", self.runs[-RUNS_KEEP:])
        write_json(self.dir / "fx.json", self.fx)

    def log_run(self, record: dict) -> None:
        self.runs.append(record)
