"""Загрузка и проверка конфигурации (config/settings.yaml, config/pages.yaml)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LANG_ORDER = ["FR", "IT", "EN", "NL", "RU", "TR", "DE", "PT", "PL", "CS", "UK", "ES", "RO", "AR"]
LANG_NAMES = {"FR": "French", "IT": "Italian", "EN": "English", "NL": "Dutch",
              "RU": "Russian", "TR": "Turkish", "DE": "German", "PT": "Portuguese", "PL": "Polish", "CS": "Czech",
              "UK": "Ukrainian", "ES": "Spanish", "RO": "Romanian", "AR": "Arabic"}
# Страницы, перенесённые из Make (у них есть make_post_module), — проверка точного переноса.
# (Moments Français | Paris в Make стояла среди IT; 29.10.2026 владелец уточнила — это французская страница)
EXPECTED_COUNTS = {"FR": 8, "IT": 8, "EN": 16, "NL": 2}


class ConfigError(Exception):
    pass


@dataclass
class Page:
    page_id: str
    name: str
    lang: str
    profile_id: str = ""
    from_make: bool = False
    disabled: bool = False
    group: str = ""          # группа страниц: своя история в каждом выпуске (по умолчанию = язык)


@dataclass
class Config:
    root: Path
    settings: dict
    languages: dict  # lang -> {"name": str, "pages": [Page]}
    fallback_source_page_id: str = ""
    pages: list = field(default_factory=list)       # страницы, на которые идёт публикация
    all_pages: list = field(default_factory=list)   # включая отключённые (disabled: true)
    group: str = ""                                 # не пусто — это «вид» одной группы (см. group_view)

    # --- удобные свойства -------------------------------------------------
    @property
    def tz(self) -> str:
        return self.settings.get("timezone", "Asia/Yerevan")

    @property
    def slots(self) -> list:
        return list(self.settings.get("slots") or [])

    @property
    def timing(self) -> dict:
        return self.settings.get("timing") or {}

    @property
    def site(self) -> dict:
        return self.settings.get("site") or {}

    @property
    def claude(self) -> dict:
        return self.settings.get("claude") or {}

    @property
    def sources(self) -> dict:
        return self.settings.get("sources") or {}

    @property
    def graph_version(self) -> str:
        return (self.settings.get("facebook") or {}).get("graph_version", "v26.0")

    def lang_name(self, lang: str) -> str:
        return self.languages[lang]["name"]

    def pages_for(self, lang: str) -> list:
        return list(self.languages[lang]["pages"])

    def groups(self) -> list:
        """Группы страниц по порядку языков: у каждой своя очередь ссылок и своя история в выпуске."""
        out = []
        for lg in LANG_ORDER:
            for p in self.languages[lg]["pages"]:
                if p.group not in out:
                    out.append(p.group)
        return out

    def group_label(self, g: str) -> str:
        if g == "EN" and "EN2" in self.groups():
            return "EN-1"
        if g == "IT" and "IT2" in self.groups():
            return "IT-1"
        langs = self.group_langs(g)
        if g == "MIX":              # так её называет владелица: «Mix»
            return "Mix (" + "+".join(langs) + ")"
        if len(langs) > 1:          # группа из нескольких языков: одна история на всех, например RU+TR+PT
            return "+".join(langs)
        return {"EN2": "EN-2", "IT2": "IT-2"}.get(g, g)

    def group_langs(self, g: str) -> list:
        return [lg for lg in LANG_ORDER if any(p.group == g for p in self.languages[lg]["pages"])]

    def active_langs(self) -> list:
        """Языки, у которых есть хотя бы одна страница."""
        return [lg for lg in LANG_ORDER if self.languages[lg]["pages"]]

    def page(self, page_id: str) -> Page | None:
        for p in self.pages:
            if p.page_id == str(page_id):
                return p
        return None

    def profile_to_page(self) -> dict:
        return {p.profile_id: p.page_id for p in self.all_pages if p.profile_id}

    def test_page_id(self) -> str:
        tp = str(self.settings.get("test_page_id") or "").strip()
        if tp:
            return tp
        return self.pages_for("NL")[0].page_id

    def path(self, rel: str) -> Path:
        return self.root / rel


def _validate_slots(slots: list) -> None:
    if not slots:
        raise ConfigError("settings.yaml: список slots пуст")
    seen = set()
    for s in slots:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(s)):
            raise ConfigError(f"settings.yaml: неверное время слота '{s}', нужно ЧЧ:ММ")
        if s in seen:
            raise ConfigError(f"settings.yaml: слот '{s}' повторяется")
        seen.add(s)


def load(root: str | Path | None = None) -> Config:
    root = Path(root or os.environ.get("AUTOPOST_ROOT") or ROOT)
    settings = yaml.safe_load((root / "config/settings.yaml").read_text(encoding="utf-8")) or {}
    pages_doc = yaml.safe_load((root / "config/pages.yaml").read_text(encoding="utf-8")) or {}

    languages = {lg: {"name": LANG_NAMES[lg], "pages": []} for lg in LANG_ORDER}
    all_pages, every_page = [], []
    for lang in LANG_ORDER:
        block = (pages_doc.get("languages") or {}).get(lang) or {"name": LANG_NAMES[lang], "pages": []}
        languages[lang]["name"] = block.get("name", LANG_NAMES[lang])
        for item in block.get("pages") or []:
            real = str(item.get("lang") or lang)   # lang: FR — язык страницы исправлен (в Make был другой)
            if real not in languages:
                raise ConfigError(f"pages.yaml: неизвестный язык {real} у страницы {item.get('name')}")
            p = Page(page_id=str(item["page_id"]), name=str(item["name"]), lang=real,
                     profile_id=str(item.get("profile_id") or ""), from_make=bool(item.get("make_post_module")),
                     disabled=bool(item.get("disabled")), group=str(item.get("group") or real))
            every_page.append(p)
            if p.disabled:
                continue
            languages[real]["pages"].append(p)
            all_pages.append(p)

    ids = [p.page_id for p in every_page]
    if len(ids) != len(set(ids)):
        raise ConfigError("pages.yaml: одна и та же страница указана дважды")
    _validate_slots(settings.get("slots") or [])
    sorted_slots = sorted(settings["slots"])
    settings["slots"] = sorted_slots

    cfg = Config(root=root, settings=settings, languages=languages,
                 fallback_source_page_id=str(pages_doc.get("fallback_source_page_id") or ""),
                 pages=all_pages, all_pages=every_page)
    if cfg.page(cfg.test_page_id()) is None:
        raise ConfigError(f"settings.yaml: test_page_id {cfg.test_page_id()} нет среди страниц pages.yaml")
    return cfg


def check_expected_counts(cfg: Config) -> list:
    """Возвращает список расхождений с исходным файлом Make (пусто — всё совпадает)."""
    problems = []
    if not any(p.from_make for p in cfg.all_pages):
        return problems          # отдельная система: страниц из Make нет, сверять не с чем
    for lang, n in EXPECTED_COUNTS.items():
        got = sum(1 for p in cfg.all_pages if p.lang == lang and p.from_make)
        if got != n:
            problems.append(f"{lang}: страниц из Make {got}, в Make было {n}")
    made = sum(1 for p in cfg.all_pages if p.from_make)
    if made != 34:
        problems.append(f"страниц из Make {made}, в Make было 34")
    return problems


def describe(cfg: Config) -> str:
    """Например: «54 страницы: FR 7 · IT 9 · EN 29 · NL 2 · RU 3 · TR 2 · DE 2»."""
    parts = " · ".join(f"{lg} {len(cfg.pages_for(lg))}" for lg in cfg.active_langs())
    off = len(cfg.all_pages) - len(cfg.pages)
    return f"{len(cfg.pages)} стр.: {parts}" + (f" (отключено: {off})" if off else "")


def site_for(settings: dict, g: str) -> dict:
    """Сайт WordPress группы: общий блок site, поверх него — group_sites.<группа> (если задан)."""
    site = dict(settings.get("site") or {})
    site.update((settings.get("group_sites") or {}).get(g) or {})
    return site


def group_view(cfg: Config, g: str, page_ids=None) -> Config:
    """Конфигурация одной группы: только её страницы, своя очередь queue/<группа>.txt, только присланные ссылки.

    page_ids — «завершающий» вид прежней группы (её страницы уже перенесены в другие группы): только эти
    страницы и только уже подготовленные выпуски — дописать комментарии и закрыть слоты."""
    import copy
    pick = (lambda p: p.page_id in page_ids) if page_ids is not None else (lambda p: p.group == g)
    langs = {lg: {"name": v["name"], "pages": [p for p in v["pages"] if pick(p)]}
             for lg, v in cfg.languages.items()}
    settings = copy.deepcopy(cfg.settings)
    settings["site"] = site_for(cfg.settings, g)
    if page_ids is not None:
        settings["finish_only"] = True
    src = settings.setdefault("sources", {})
    src["order"] = ["manual"]
    src["manual_file"] = f"queue/{g}.txt"
    src.setdefault("auto", {})["enabled"] = False
    src.setdefault("google_sheet", {})["enabled"] = False
    return Config(root=cfg.root, settings=settings, languages=langs,
                  fallback_source_page_id=cfg.fallback_source_page_id,
                  pages=[p for p in cfg.pages if pick(p)], all_pages=cfg.all_pages, group=g)


def group_state_path(cfg: Config, g: str) -> Path:
    return cfg.path(f"state/groups/{g}.json")
