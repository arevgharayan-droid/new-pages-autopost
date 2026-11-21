"""Состояние: что уже сделано, чтобы не было дублей и повторялись только неудавшиеся шаги.

Файл state/state.json сохраняется после КАЖДОГО внешнего действия (атомарно),
а GitHub Actions в конце любого запуска (даже упавшего) коммитит его в репозиторий.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = 1


def text_fingerprint(text: str) -> str:
    """Отпечаток текста, нечувствительный к регистру, пробелам и пунктуации."""
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[\W_]+", " ", t).strip()
    t = t[:400]
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:20]


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {}
        self.data.setdefault("version", VERSION)
        self.data.setdefault("sources", {})      # source_key -> info
        self.data.setdefault("slots", {})        # "YYYY-MM-DD HH:MM" -> slot
        self.data.setdefault("outputs", {})      # fb object id -> iso time (наши посты)
        self.data.setdefault("output_fps", {})   # отпечаток нашего текста -> iso time
        self.data.setdefault("tests", {})

    # --- запись -----------------------------------------------------------
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    # --- граница «новых» историй -----------------------------------------
    def ensure_source_cutoff(self, now: datetime) -> str:
        """Посты, созданные раньше этого момента (в т.ч. всё, что публиковал Make), не берутся как новые."""
        if not self.data.get("source_cutoff"):
            self.data["source_cutoff"] = now.astimezone(timezone.utc).isoformat(timespec="seconds")
        return self.data["source_cutoff"]

    # --- источники --------------------------------------------------------
    @property
    def sources(self) -> dict:
        return self.data["sources"]

    def source_used(self, key: str) -> bool:
        s = self.sources.get(key)
        return bool(s and s.get("status") in ("used", "skipped", "failed"))

    def fingerprint_used(self, fp: str) -> bool:
        for s in self.sources.values():
            if s.get("fingerprint") == fp and s.get("status") in ("used", "reserved"):
                return True
        return False

    def mark_source(self, key: str, **fields) -> dict:
        s = self.sources.setdefault(key, {})
        s.update(fields)
        self.save()
        return s

    # --- наши публикации (чтобы авто-режим не взял их как новые истории) ---
    def remember_output(self, obj_id: str | None, text: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if obj_id:
            self.data["outputs"][str(obj_id)] = now
        if text:
            self.data["output_fps"][text_fingerprint(text)] = now

    def is_our_output(self, obj_id: str, text: str | None) -> bool:
        if str(obj_id) in self.data["outputs"]:
            return True
        # пост страницы может иметь id вида PAGE_POST; сверяем и хвост
        tail = str(obj_id).split("_")[-1]
        if tail in self.data["outputs"]:
            return True
        return bool(text) and text_fingerprint(text) in self.data["output_fps"]

    # --- слоты ------------------------------------------------------------
    @property
    def slots(self) -> dict:
        return self.data["slots"]

    def slot(self, key: str) -> dict:
        return self.slots.setdefault(key, {"status": "new", "langs": {}})

    def prune(self, keep_days: int, now: datetime) -> None:
        cutoff = now - timedelta(days=keep_days)
        for key in list(self.slots):
            try:
                dt = datetime.fromisoformat(self.slots[key].get("slot_iso"))
            except (TypeError, ValueError):
                continue
            if dt < cutoff:
                del self.slots[key]
        for bucket in ("outputs", "output_fps"):
            for k, iso in list(self.data[bucket].items()):
                try:
                    if datetime.fromisoformat(iso) < cutoff:
                        del self.data[bucket][k]
                except ValueError:
                    pass
