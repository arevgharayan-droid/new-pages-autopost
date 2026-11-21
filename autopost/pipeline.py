"""Основной конвейер: подготовка слота → статьи WordPress → отложенные посты FB → первый комментарий.

Защита от дублей в два слоя:
1. state/state.json сохраняется после каждого действия и коммитится в репозиторий (в т.ч. по ходу работы);
2. перед КАЖДЫМ созданием система проверяет саму площадку: статья ищется по постоянному ярлыку
   (slug), пост — среди отложенных/опубликованных постов страницы, комментарий — среди комментариев.
   Поэтому даже потеря state.json или повторный запуск не создают дублей.
"""
from __future__ import annotations

import html
import json
import mimetypes
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from .config import LANG_ORDER
from .fb import FacebookError, redact
from .generate import GenerationError
from .sources import SourceError, pick_source
from .state import text_fingerprint
from .wp import WordPressError

MAX_FB_ATTEMPTS = 4
AUTH_FAILURES_TO_ABORT = 4
DONE_PAGE = {"done", "skipped_source", "failed_final", "no_access"}
# страница ещё не подключена (нет токена) — пропускается без ошибки, пока её не подключат
OK_PAGE = ("done", "skipped_source", "no_access")
LIVE_PAGE = {"scheduled", "published"}
FINAL_SLOT = {"done", "done_with_errors", "missed", "cancelled"}


class AuthProblem(Exception):
    pass


class Report:
    def __init__(self):
        self.lines, self.errors, self.final_errors = [], [], []
        self.counts = {"wp_created": 0, "fb_scheduled": 0, "fb_published": 0, "comments": 0, "generated": 0}

    def info(self, msg):
        msg = redact(msg)
        self.lines.append(msg)
        print(msg, flush=True)

    def error(self, msg, final=False):
        msg = redact(msg)
        (self.final_errors if final else self.errors).append(msg)
        print(("❌ " if final else "⚠️ ") + msg, flush=True)

    def markdown(self, title="Запуск автопостинга") -> str:
        c = self.counts
        out = [f"## {title}", "",
               f"Статей WordPress создано: **{c['wp_created']}** · текстов сгенерировано: **{c['generated']}** · "
               f"постов FB запланировано: **{c['fb_scheduled']}** · опубликовано сразу: **{c['fb_published']}** · "
               f"первых комментариев: **{c['comments']}**", ""]
        if self.final_errors:
            out += ["### ❌ Требует внимания", *[f"- {e}" for e in self.final_errors], ""]
        if self.errors:
            out += ["### ⚠️ Временные ошибки (будут повторены автоматически)", *[f"- {e}" for e in self.errors[:60]], ""]
        if self.lines:
            out += ["### Ход работы", *[f"- {line}" for line in self.lines[-120:]]]
        return "\n".join(out) + "\n"


def _without_outro(message: str) -> str:
    """Текст комментария без строки-призыва «поставьте лайк и напишите „Да“» (формат «по частям»)."""
    from .generate import COMMENT_OUTRO
    outros = set(COMMENT_OUTRO.values())
    parts = message.split("\n\n")
    kept = [p for p in parts if p.strip() not in outros]
    return "\n\n".join(kept)


class Pacer:
    """Ровный темп записей в Facebook (посты, комментарии) — общий для всех групп одного запуска:
    не чаще одной записи в `seconds` секунд (+ немного случайности), чтобы не было всплеска.

    Плюс «предохранитель» по системным пользователям (owners: page_id → кто публикует): если Facebook
    ответил одному пользователю «ограничиваем частоту», останавливается только он — во всех группах сразу,
    на паузу 60 мин (каждый новый отказ удваивает, до 8 ч); другой пользователь продолжает.
    path — общий файл пауз (state/fb_guard.json); без него паузы хранятся в состоянии группы."""

    def __init__(self, seconds: float = 0.0, owners: dict | None = None, path=None, per_owner: dict | None = None):
        self.seconds = float(seconds or 0)
        self._lock = threading.Lock()
        self._last = 0.0
        # свой, более медленный темп для отдельных пользователей (например, старому autopost — раз в 45 с)
        self.per_owner = {k: float(v) for k, v in (per_owner or {}).items() if v}
        self._olocks = {k: threading.Lock() for k in self.per_owner}
        self._olast = {}
        self.owners = dict(owners or {})
        self.path = path
        self.blocks = {}
        self.stopped = set()      # кому в этом запуске Facebook уже отказал
        if path is not None and path.exists():
            try:
                self.blocks = json.loads(path.read_text(encoding="utf-8")) or {}
            except ValueError:
                self.blocks = {}

    def owner(self, page_id) -> str:
        return self.owners.get(page_id, "main")

    def blocked_until(self, owner, now):
        u = (self.blocks.get(owner) or {}).get("until")
        if u and now < datetime.fromisoformat(u):
            return datetime.fromisoformat(u)
        return None

    def block(self, owner, now) -> int:
        with self._lock:
            b = self.blocks.setdefault(owner, {})
            if owner in self.stopped:
                # другие группы этого же запуска получили отказ одновременно — это один и тот же отказ
                return int(b.get("minutes") or 60)
            self.stopped.add(owner)
            prev, last = int(b.get("minutes") or 0), b.get("until")
            recent = last and now - datetime.fromisoformat(last) < timedelta(hours=6)
            minutes = min(480, prev * 2) if (prev and recent) else 60
            b.update(minutes=minutes, until=(now + timedelta(minutes=minutes)).isoformat(timespec="seconds"))
            self._save()
            return minutes

    def ok(self, owner):
        b = self.blocks.get(owner)
        if b and b.get("minutes"):
            with self._lock:
                b.pop("minutes", None)   # записи снова проходят — следующая пауза снова с 60 мин
                self._save()

    def _save(self):
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.blocks, ensure_ascii=False, indent=1), encoding="utf-8")

    def wait(self, owner: str | None = None):
        og = self.per_owner.get(owner) if owner else None
        if og:
            with self._olocks[owner]:
                gap = self._olast.get(owner, 0.0) + og + random.uniform(0, og * 0.3) - time.monotonic()
                if gap > 0:
                    time.sleep(gap)
                self._olast[owner] = time.monotonic()
        if self.seconds <= 0:
            return
        with self._lock:
            gap = self._last + self.seconds + random.uniform(0, self.seconds * 0.3) - time.monotonic()
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


class Pipeline:
    def __init__(self, cfg, state, graph, wp, gen, tokens: dict, now_fn=None, http=None, report=None,
                 checkpoint=None, wp_factory=None, pacer=None):
        self.cfg, self.state, self.graph, self.wp, self.gen = cfg, state, graph, wp, gen
        # wp_factory(site) — клиент другого сайта: выпуск, начатый на одном сайте, там и заканчивается,
        # даже если сайт группы с тех пор поменяли
        self.wp_factory = wp_factory
        self._wps = {}
        self.pacer = pacer or Pacer(0)
        if self.pacer.path is None:
            # без общего файла паузы живут в состоянии группы (сохраняются вместе с ним)
            self.pacer.blocks = state.data.setdefault("fb_blocks", {})
        self._told_block = set()
        self.tokens = tokens
        self.tz = ZoneInfo(cfg.tz)
        self.now_fn = now_fn or (lambda: datetime.now(self.tz))
        self.http = http or requests.Session()
        self.r = report or Report()
        self.checkpoint = checkpoint or (lambda: None)
        self._images = {}
        self._remote_cache = {}
        self.auth_failures = 0
        t = cfg.timing
        self.ahead = timedelta(minutes=int(t.get("prepare_ahead_minutes", 200)))
        self.min_lead = timedelta(minutes=int(t.get("min_schedule_lead_minutes", 15)))
        self.late = timedelta(minutes=int(t.get("late_publish_window_minutes", 90)))
        # новую историю в выпуск, час которого уже прошёл, берём только в первые N минут —
        # иначе ссылка ждёт следующего выпуска (опоздание до late — только для уже взятой истории)
        self.new_story_late = timedelta(minutes=int(t.get("new_story_late_minutes", 30)))
        self.final_after = self.late + timedelta(hours=8)  # комментарии повторяются до 12 ч после выпуска

    # ------------------------------------------------------------------ сайт выпуска
    SITE_KEYS = ("url", "comment_link_format", "wp_auth", "key_secret", "user_secret", "password_secret")

    def site_rec(self) -> dict:
        return {k: self.cfg.site[k] for k in self.SITE_KEYS if self.cfg.site.get(k)}

    def slot_site(self, s: dict) -> dict:
        return s.get("wp_site") or self.site_rec()

    def wp_of(self, s: dict):
        site = s.get("wp_site") or {}
        url = site.get("url")
        if not url or url == self.cfg.site.get("url") or not self.wp_factory:
            return self.wp
        if url not in self._wps:
            self._wps[url] = self.wp_factory(site)
        return self._wps[url]

    # ------------------------------------------------------------------ helpers
    def now(self) -> datetime:
        return self.now_fn().astimezone(self.tz)

    def slot_dt(self, day, hhmm: str) -> datetime:
        h, m = map(int, hhmm.split(":"))
        return datetime(day.year, day.month, day.day, h, m, tzinfo=self.tz)

    @staticmethod
    def key(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d %H:%M")

    def log_event(self, **ev):
        ev["t"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if self.cfg.group:
            ev["group"] = self.cfg.group
        p = self.cfg.path(f"logs/{ev['t'][:7]}.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(redact(json.dumps(ev, ensure_ascii=False)) + "\n")

    def _download(self, url: str) -> tuple:
        try:
            r = self.http.get(url, timeout=60)
        except requests.RequestException as e:
            raise SourceError(f"не удалось скачать изображение: {type(e).__name__}")
        if r.status_code != 200 or not r.content:
            raise SourceError(f"не удалось скачать изображение (HTTP {r.status_code})")
        mime = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
        if not mime.startswith("image/"):
            raise SourceError(f"по ссылке не изображение ({mime})")
        return r.content, mime

    def image_bytes(self, key: str, s: dict) -> tuple:
        if key not in self._images:
            src = s["source"]
            url = src.get("wp_media_url") or src["image_url"]
            self._images[key] = self._download(url)
        return self._images[key]

    def content_path(self, dt: datetime, lang: str):
        if self.cfg.group:
            return self.cfg.path(f"content/{dt:%Y-%m-%d}/{self.cfg.group}/{dt:%H%M}-{lang}.json")
        return self.cfg.path(f"content/{dt:%Y-%m-%d}/{dt:%H%M}-{lang}.json")

    def load_text(self, dt: datetime, lang: str) -> dict:
        return json.loads(self.content_path(dt, lang).read_text(encoding="utf-8"))

    def page_error(self, key, page_name, e: dict, err, what: str):
        """Ошибка одной страницы не останавливает остальные 33."""
        msg = err.short() if isinstance(err, FacebookError) else str(err)
        e["error"] = redact(f"{what}: {msg}")
        if isinstance(err, FacebookError) and err.auth:
            self.auth_failures += 1
            e["error"] = "токен страницы недействителен — повторите «2 · Подключить Facebook»"
            if self.auth_failures >= AUTH_FAILURES_TO_ABORT:
                raise AuthProblem("Facebook отклоняет токены сразу нескольких страниц. "
                                  "Запустите workflow «2 · Подключить Facebook» заново.")
        self.r.error(f"{key} «{page_name}»: {e['error']}")
        self.state.save()

    # ------------------------------------------------------------------ tick
    def has_unfinished_prepared(self) -> bool:
        return any(s.get("prepared") and s.get("status") not in FINAL_SLOT for s in self.state.slots.values())

    def tick(self) -> Report:
        now = self.now()
        st = self.state.data
        enabled = bool(self.cfg.settings.get("schedule_enabled"))
        if enabled and not st.get("enabled_since"):
            st["enabled_since"] = now.isoformat(timespec="seconds")
        if not enabled:
            st["enabled_since"] = None
            self.r.info("Расписание выключено — новые истории не готовлю, только завершаю уже подготовленные слоты.")
        self.state.ensure_source_cutoff(now)
        self.state.save()
        enabled_since = datetime.fromisoformat(st["enabled_since"]) if st.get("enabled_since") else None
        days = [(now - timedelta(days=1)).date(), now.date(), (now + timedelta(days=1)).date()]
        dts = {self.slot_dt(d, s) for d in days for s in self.cfg.slots}
        # внеочередные выпуски («опубликовать сейчас») — в своё время, вне сетки расписания
        dts |= {datetime.fromisoformat(x["slot_iso"]) for x in self.state.slots.values()
                if x.get("adhoc") and x.get("slot_iso") and x.get("status") not in FINAL_SLOT}
        dts = sorted(dts)
        for dt in dts:
            key = self.key(dt)
            existing = self.state.slots.get(key)
            if dt - self.ahead > now:
                continue
            if existing and existing.get("status") in FINAL_SLOT:
                continue
            if not enabled and not (existing and existing.get("prepared")):
                continue
            if self.cfg.settings.get("finish_only") and not (existing and existing.get("prepared")):
                continue   # прежняя группа: только дописываем уже подготовленные выпуски
            if enabled and not existing and dt < enabled_since:
                continue
            try:
                self.process_slot(dt, allow_new=enabled)
            except AuthProblem as e:
                self.r.error(str(e), final=True)
                break
            except Exception as e:  # noqa: BLE001 — сбой одного слота не должен остановить остальные
                self.r.error(f"{key}: непредвиденная ошибка {type(e).__name__}: {e}")
            self.checkpoint()
        self.state.prune(int(self.cfg.timing.get("keep_state_days", 14)), now)
        self.state.save()
        return self.r

    # ------------------------------------------------------------------ slot
    def process_slot(self, dt: datetime, allow_new=True):
        now = self.now()
        key = self.key(dt)
        s = self.state.slot(key)
        s.setdefault("slot_iso", dt.isoformat())
        if (s.get("prepared") or s.get("source_key")) and not s.get("wp_site"):
            s["wp_site"] = self.site_rec()   # выпуск запоминает свой сайт
        if now > dt + self.final_after:
            # последний шанс: выпущенным постам — комментарии, затем закрываем слот
            if s.get("prepared"):
                self.finish_after_release(dt, s)
                self.close_slot(dt, final=True)
            else:
                self.mark_missed(key, s)
            return
        if not s.get("prepared"):
            if not allow_new:
                return
            if now > dt + self.late:
                self.mark_missed(key, s)
                return
            if not s.get("source_key") and now > dt + self.new_story_late:
                self.mark_missed(key, s)   # история для этого часа так и не пришла — ссылки ждут следующего выпуска
                return
            if not self.prepare(dt, s):
                return
            self.checkpoint()
        if allow_new:
            self.publish_pages(dt, s)
        if now >= dt:
            self.finish_after_release(dt, s)
        self.close_slot(dt)

    def mark_missed(self, key, s):
        if s.get("status") == "no_source" or not s.get("source_key"):
            self.r.error(f"{key}: слот пропущен — не было новой истории")
        else:
            self.r.error(f"{key}: слот пропущен — история не была подготовлена вовремя", final=True)
            self.state.mark_source(s["source_key"], status="failed", error="слот пропущен")
        s["status"] = "missed"
        self.state.save()

    # ---- подготовка: история, фото, тексты, статьи -----------------------------
    def prepare(self, dt: datetime, s: dict) -> bool:
        key = self.key(dt)
        now = self.now()
        if not s.get("source_key"):
            restored = self.restore_from_content(dt, s)
            if restored:
                self.r.info(f"{key}: состояние восстановлено из сохранённых текстов")
        if not s.get("source_key"):
            try:
                src = pick_source(self.cfg, self.graph, self.tokens, self.state, now, self.r.info)
            except FacebookError as e:
                self.r.error(f"{key}: источники недоступны: {e.short()}")
                return False
            if not src:
                if s.get("status") != "no_source":
                    self.r.error(f"{key}: нет новой истории для публикации (очередь пуста)")
                s["status"] = "no_source"
                self.state.save()
                return False
            s["source_key"] = src["key"]
            s["source"] = {k: src[k] for k in ("page_id", "text", "image_url", "origin", "link")}
            s["status"] = "preparing"
            self.state.mark_source(src["key"], status="reserved", slot=key, link=src["link"],
                                   origin=src["origin"], fingerprint=src["fingerprint"], page_id=src["page_id"])
            self.r.info(f"{key}: выбрана история ({src['origin']}) {src['link'][:90]}")
        s.setdefault("wp_site", self.site_rec())
        wp = self.wp_of(s)
        src = s["source"]
        # фото → медиатека WordPress (постоянная ссылка вместо временной ссылки Facebook)
        if not src.get("wp_media_id"):
            slug = f"interes-{dt:%Y%m%d-%H%M}" + (f"-{self.cfg.group.lower()}" if self.cfg.group else "")
            try:
                found = wp.find_media_by_slug(slug)
                if found:
                    media = found[0]
                else:
                    data, mime = self._download(src["image_url"])
                    ext = mimetypes.guess_extension(mime) or ".jpg"
                    media = wp.upload_media(data, f"{slug}{ext}", mime)
                    self._images[key] = (data, mime)
                src["wp_media_id"], src["wp_media_url"] = media["id"], media.get("source_url")
                self.state.save()
                self.log_event(slot=key, action="wp_media", id=media["id"])
            except (SourceError, WordPressError) as e:
                self.r.error(f"{key}: изображение: {e}")
                return False
        # язык, у которого ещё ни одна страница не подключена, пропускаем целиком:
        # без текста и без статьи на сайте (иначе статья вышла бы без единого поста)
        for lang in LANG_ORDER:
            L = s["langs"].setdefault(lang, {"pages": {}})
            if L.get("generated") or L.get("skipped"):
                continue
            if not any(self.tokens.get(p.page_id) for p in self.cfg.pages_for(lang)):
                L["skipped"] = "нет подключённых страниц"
                for page in self.cfg.pages_for(lang):
                    L["pages"][page.page_id] = {"status": "no_access", "attempts": 0}
                if self.cfg.pages_for(lang):  # о языках, которых в группе нет вовсе, не сообщаем
                    self.r.info(f"{key} {lang}: страницы этого языка пока не подключены — язык пропущен")
        active = [lg for lg in LANG_ORDER if not s["langs"][lg].get("skipped")]
        if not active:
            self.r.error(f"{key}: ни одна страница не подключена — нечего публиковать", final=True)
            return False
        # тексты: языки параллельно (уже сохранённые тексты не генерируются заново)
        for lang in active:
            L = s["langs"].setdefault(lang, {"pages": {}})
            if not L.get("generated") and self.content_path(dt, lang).exists():
                t = self.load_text(dt, lang)
                if t.get("source_key") == s["source_key"]:
                    L.update(generated=True, title=t["title"], backend=t.get("backend"), gen_error=None)
        todo = [lang for lang in active if not s["langs"].get(lang, {}).get("generated")]
        if todo:
            def work(lang):
                try:
                    return lang, self.gen.generate(lang, src["text"]), None
                except GenerationError as e:
                    return lang, None, e
            with ThreadPoolExecutor(max_workers=max(1, len(todo))) as pool:
                results = list(pool.map(work, todo))
            for lang, g, err in results:
                L = s["langs"].setdefault(lang, {"pages": {}})
                if err:
                    L["gen_error"] = str(err)
                    L["gen_attempts"] = L.get("gen_attempts", 0) + 1
                    self.r.error(f"{key} {lang}: {err}")
                    continue
                p = self.content_path(dt, lang)
                p.parent.mkdir(parents=True, exist_ok=True)
                payload = g.as_dict() | {"lang": lang, "slot": key, "source_key": s["source_key"],
                                         "source_link": src["link"], "source": {
                                             k: src.get(k) for k in ("page_id", "text", "image_url", "origin", "link")}}
                p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
                L.update(generated=True, title=g.title, backend=g.backend, warnings=g.warnings, gen_error=None)
                self.state.remember_output(None, g.fb_text)
                self.r.counts["generated"] += 1
                self.log_event(slot=key, lang=lang, action="generated", backend=g.backend, warnings=g.warnings)
            self.state.save()
        # статьи WordPress
        for lang in active:
            L = s["langs"].get(lang)
            if L and L.get("generated") and not L.get("wp_id"):
                self.create_wp(dt, s, lang, L)
        ready_langs = [lg for lg in active if s["langs"].get(lg, {}).get("wp_id")]
        ready = len(ready_langs) == len(active)
        if not ready and ready_langs and now >= dt - self.min_lead:
            # Время вышло: публикуем готовые языки, остальные отмечаем как неудавшиеся.
            for lg in active:
                if lg in ready_langs:
                    continue
                L = s["langs"].setdefault(lg, {"pages": {}})
                why = L.get("gen_error") or L.get("wp_error") or "не подготовлено"
                for page in self.cfg.pages_for(lg):
                    L["pages"][page.page_id] = {"status": "failed_final", "attempts": 0, "error": why}
                self.r.error(f"{key} {lg}: язык пропущен в этом слоте: {why}", final=True)
            ready = True
        if ready:
            s["prepared"] = True
            s["status"] = "prepared"
            self.state.mark_source(s["source_key"], status="used", slot=key)
        self.state.save()
        return ready

    def restore_from_content(self, dt, s) -> bool:
        """Если state.json потерян, но тексты слота уже сохранены — продолжаем с ними (без новой генерации)."""
        for lang in LANG_ORDER:
            p = self.content_path(dt, lang)
            if p.exists():
                t = json.loads(p.read_text(encoding="utf-8"))
                if t.get("source") and t.get("source_key"):
                    s["source_key"] = t["source_key"]
                    s["source"] = dict(t["source"])
                    s["status"] = "preparing"
                    self.state.mark_source(t["source_key"], status="reserved", slot=self.key(dt),
                                           link=t["source"].get("link"), origin=t["source"].get("origin"),
                                           fingerprint=text_fingerprint(t["source"].get("text") or ""),
                                           page_id=t["source"].get("page_id"))
                    return True
        return False

    def wp_slug(self, dt, lang) -> str:
        if self.cfg.group:
            base = f"iw-{dt:%Y%m%d-%H%M}-{self.cfg.group.lower()}"
            # в группе из нескольких языков у каждого языка своя статья
            return base + (f"-{lang.lower()}" if len({p.lang for p in self.cfg.pages}) > 1 else "")
        return f"iw-{dt:%Y%m%d-%H%M}-{lang.lower()}"

    def create_wp(self, dt, s, lang, L):
        key = self.key(dt)
        text = self.load_text(dt, lang)
        src = s["source"]
        slug = self.wp_slug(dt, lang)
        try:
            found = self.wp_of(s).find_by_slug(slug)
        except WordPressError as e:
            L["wp_error"] = f"проверка перед созданием: {e}"
            self.r.error(f"{key} {lang}: WordPress недоступен для проверки, повторю позже: {e}")
            return
        if found:
            L.update(wp_id=found[0]["id"], wp_status=found[0].get("status"), wp_error=None)
            self.state.save()
            return
        future = self.cfg.site.get("wp_status_mode", "future") == "future" and dt > self.now() + timedelta(minutes=2)
        img = (f'<figure class="wp-block-image size-full"><img src="{html.escape(src.get("wp_media_url") or "")}" '
               f'alt="{html.escape(text["title"])}"/></figure>\n')
        try:
            post = self.wp_of(s).create_post(
                title=text["title"], content=img + text["article_html"], slug=slug,
                status="future" if future else "publish",
                date_gmt=dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") if future else None,
                featured_media=src.get("wp_media_id") if self.cfg.site.get("wp_featured_image") else None,
                categories=(self.cfg.site.get("wp_categories") or {}).get(lang))
        except WordPressError as e:
            # Исход мог быть успешным (например, 502 от прокси) — в следующий раз статья найдётся по slug.
            L["wp_error"] = str(e)
            self.r.error(f"{key} {lang}: WordPress: {e}")
            self.state.save()
            return
        L.update(wp_id=post["id"], wp_status=post.get("status"), wp_error=None)
        self.r.counts["wp_created"] += 1
        self.log_event(slot=key, lang=lang, action="wp_post", id=post["id"], status=post.get("status"))
        self.state.save()

    # ---- публикация на страницах ----------------------------------------------
    def publish_pages(self, dt: datetime, s: dict, only_page: str | None = None):
        key = self.key(dt)
        now = self.now()
        if now <= dt - self.min_lead:
            mode = "schedule"
        elif dt <= now <= dt + self.late:
            mode = "immediate"
        elif now < dt:
            return  # слишком поздно ставить в отложенные, слишком рано публиковать — ждём
        else:
            mode = "expired"
        if mode == "immediate" and self._newer_release_started(dt, now):
            mode = "expired"   # следующий выпуск уже начался — старую историю поверх новой не публикуем
        skip_src = self.cfg.sources.get("skip_source_page", True)
        until = self.state.data.get("fb_block_until")
        if until and now < datetime.fromisoformat(until):
            self.r.info(f"{key}: Facebook временно ограничил публикации — пауза до "
                        f"{datetime.fromisoformat(until).astimezone(self.tz):%H:%M}, потом повторю")
            return
        # два прохода: сначала страницы обычного темпа, потом медленного пользователя — чтобы он их не задерживал
        for slow_pass in (False, True):
          for lang in LANG_ORDER:
            L = s["langs"].setdefault(lang, {"pages": {}})
            if not L.get("generated"):
                # язык не готовился в этом слоте (страницы подключили позже) — его страницы ждут следующего слота
                for page in self.cfg.pages_for(lang):
                    L["pages"].setdefault(page.page_id, {"status": "no_access", "attempts": 0})
                continue
            text = None
            for page in self.cfg.pages_for(lang):
                if only_page and page.page_id != only_page:
                    continue
                if self._slow(page.page_id) != slow_pass:
                    continue
                e = L["pages"].setdefault(page.page_id, {"status": "new", "attempts": 0})
                if e["status"] in DONE_PAGE or e["status"] in LIVE_PAGE:
                    continue
                if skip_src and page.page_id == s["source"].get("page_id"):
                    e["status"] = "skipped_source"
                    continue
                text = text or self.load_text(dt, lang)
                tok = self.tokens.get(page.page_id)
                if not tok:
                    e["status"] = "no_access"
                    self.r.info(f"{key} {lang} «{page.name}»: страница пока не подключена — пропущена")
                    self.state.save()
                    continue
                if self._owner_paused(key, page.page_id, now):
                    continue          # этому системному пользователю Facebook ограничил частоту — ждём
                # 1) сначала проверяем площадку: может, пост уже есть (прошлый запуск оборвался)
                found = self.find_existing_post(dt, page, e, text, tok)
                if found in ("found", "unknown"):
                    continue
                # 2) лимиты
                if mode == "expired" or e.get("attempts", 0) >= MAX_FB_ATTEMPTS:
                    e["status"] = "failed_final"
                    self.r.error(f"{key} {lang} «{page.name}»: пост не опубликован: {e.get('error')}", final=True)
                    self.state.save()
                    continue
                if self.pacer.owner(page.page_id) in self.pacer.stopped:
                    continue          # Facebook ограничил частоту — страницы этого пользователя не дёргаем
                if mode == "schedule" and self.now() > dt - timedelta(minutes=11):
                    continue          # медленный темп: отложить уже нельзя (Facebook — минимум 10 мин), выйдет сразу в срок
                if mode == "immediate" and e.get("last_attempt"):
                    # лента страницы обновляется с задержкой — не спешим с повтором
                    if now - datetime.fromisoformat(e["last_attempt"]) < timedelta(minutes=5):
                        continue
                self.create_fb_post(dt, s, lang, page, e, text, mode, tok)

    def _remote_posts(self, page_id, tok, dt) -> list:
        ck = (page_id, self.key(dt))
        if ck not in self._remote_cache:
            items = [i | {"_kind": "scheduled"} for i in self.graph.scheduled_posts(page_id, tok)
                     if int(i.get("scheduled_publish_time") or 0) == int(dt.timestamp())]
            if self.now() >= dt - timedelta(minutes=30):
                items += [i | {"_kind": "published"} for i in
                          self.graph.page_posts(page_id, tok, int((dt - timedelta(hours=3)).timestamp()))]
            self._remote_cache[ck] = items
        return self._remote_cache[ck]

    def find_existing_post(self, dt, page, e, text, tok) -> str:
        """found — пост уже на странице; absent — точно нет; unknown — проверить не удалось (ждём)."""
        fp = text_fingerprint(text["fb_text"])
        try:
            items = self._remote_posts(page.page_id, tok, dt)
        except FacebookError as err:
            self.page_error(self.key(dt), page.name, e, err, "проверка перед публикацией")
            return "unknown"
        for it in items:
            # отложенный пост ровно на время слота ставит только эта система — он наш при любом тексте
            if it["_kind"] == "scheduled" or text_fingerprint(it.get("message") or "") == fp:
                e.update(post_id=it["id"], status="scheduled" if it["_kind"] == "scheduled" else "published",
                         error=None)
                self.state.remember_output(it["id"])
                self.state.save()
                return "found"
        return "absent"

    def create_fb_post(self, dt, s, lang, page, e, text, mode, tok):
        key = self.key(dt)
        try:
            data, mime = self.image_bytes(key, s)
        except SourceError as err:
            e.update(status="failed", error=str(err))
            self.r.error(f"{key}: {err}")
            return
        owner = self.pacer.owner(page.page_id)
        self.pacer.wait(owner)
        if owner in self.pacer.stopped:
            return            # пока ждали очереди, Facebook отказал этому пользователю в другой группе
        e["status"] = "sending"
        e["attempts"] = e.get("attempts", 0) + 1
        e["mode"] = mode
        e["last_attempt"] = self.now().isoformat(timespec="seconds")
        self.state.save()
        try:
            res = self.graph.create_photo_post(
                page.page_id, tok, data, f"story{mimetypes.guess_extension(mime) or '.jpg'}",
                text["fb_text"], scheduled_ts=int(dt.timestamp()) if mode == "schedule" else None, mime=mime)
        except FacebookError as err:
            if err.rate_limited:
                # не попытка, а пауза: ждём и повторяем, каждый новый отказ удваивает паузу (до 8 часов)
                e["status"] = "failed"
                e["attempts"] = max(0, e["attempts"] - 1)
                minutes = self._owner_block(page.page_id)
                self.page_error(key, f"{lang} {page.name}", e, err,
                                f"публикация (Facebook ограничил частоту — пауза {minutes} мин)")
                return
            # при временной ошибке пост мог создаться — следующий запуск сначала проверит страницу
            e["status"] = "sending" if err.transient else "failed"
            self.page_error(key, f"{lang} {page.name}", e, err, "публикация")
            return
        e.update(photo_id=res.get("id"), post_id=res.get("post_id"), error=None,
                 status="scheduled" if mode == "schedule" else "published")
        self.state.data.pop("fb_block_minutes", None)   # публикации снова проходят — пауза сбрасывается
        self.pacer.ok(self.pacer.owner(page.page_id))
        self.state.remember_output(res.get("post_id"))
        self.state.remember_output(res.get("id"))
        self.r.counts["fb_scheduled" if mode == "schedule" else "fb_published"] += 1
        self.log_event(slot=key, lang=lang, page=page.page_id, action=f"fb_{mode}",
                       photo_id=res.get("id"), post_id=res.get("post_id"))
        self.state.save()

    # ---- после выхода: статья опубликована + первый комментарий ----------------
    def finish_after_release(self, dt, s):
        key = self.key(dt)
        now = self.now()
        fmt = self.slot_site(s).get("comment_link_format") or self.cfg.site.get("comment_link_format")
        wp = self.wp_of(s)
        for slow_pass in (False, True):   # сначала обычный темп, потом медленный пользователь
          for lang in LANG_ORDER:
            L = s["langs"].get(lang) or {}
            pending = [(pid, e) for pid, e in L.get("pages", {}).items()
                       if (e["status"] in LIVE_PAGE or (e["status"] == "sending" and e.get("post_id")))
                       and self._slow(pid) == slow_pass]
            if not pending or not L.get("wp_id"):
                continue
            if not L.get("wp_live"):
                try:
                    post = wp.get_post(L["wp_id"])
                    if post.get("status") != "publish":
                        wp.update_post(L["wp_id"], status="publish")
                    L["wp_live"] = True
                    self.state.save()
                except WordPressError as err:
                    self.r.error(f"{key} {lang}: статья не опубликована: {err}")
                    continue
            link = fmt.format(wp_id=L["wp_id"])
            # формат «по частям»: в комментарии — часть 2, «ЧАСТЬ 3» и ссылка; иначе — только ссылка
            try:
                ctext = (self.load_text(dt, lang).get("comment_text") or "").strip()
            except (OSError, ValueError):
                ctext = ""
            message = f"{ctext}\n{link}" if ctext else link
            for pid, e in pending:
                page = self.cfg.page(pid)
                name = page.name if page else pid
                tok = self.tokens.get(pid)
                if not tok:
                    self.page_error(key, name, e, "нет токена страницы", "комментарий")
                    continue
                if self.pacer.owner(pid) in self.pacer.stopped or self._owner_paused(key, pid, now):
                    continue          # пауза у этого системного пользователя — комментарий позже
                try:
                    if not e.get("post_id") and e.get("photo_id"):
                        e["post_id"] = self.graph.page_story_id(e["photo_id"], tok)
                        self.state.save()
                    if not e.get("post_id") and page:
                        # запасной путь: ищем наш пост на странице по тексту
                        self._remote_cache.pop((pid, key), None)
                        if self.find_existing_post(dt, page, e, self.load_text(dt, lang), tok) == "found" \
                                and e.get("status") == "scheduled" and not self.graph.is_published(e["post_id"], tok):
                            continue
                    if not e.get("post_id"):
                        self.r.error(f"{key} «{name}»: Facebook ещё не выдал ID поста, повторю позже")
                        continue
                    target = e["post_id"]
                    if e["status"] != "published":
                        if not self.graph.is_published(target, tok):
                            if now > dt + timedelta(minutes=30):
                                self.r.error(f"{key} «{name}»: Facebook ещё не выпустил отложенный пост")
                            continue
                        e["status"] = "published"
                        self.state.remember_output(target)
                        self.state.save()
                    # перед комментарием всегда смотрим, нет ли его уже (защита от дублей)
                    existing = [c for c in self.graph.comments(target, tok)
                                if re.search(re.escape(link) + r"(?![\w])", c.get("message") or "")]
                    if existing:
                        e.update(comment_id=existing[0]["id"], status="done", error=None)
                        self.state.save()
                        continue
                    owner = self.pacer.owner(pid)
                    self.pacer.wait(owner)
                    if owner in self.pacer.stopped:
                        continue      # пока ждали очереди, этому пользователю отказали — комментарий позже
                    e["comment_attempts"] = e.get("comment_attempts", 0) + 1
                    self.state.save()
                    try:
                        c = self.graph.comment(target, tok, message)
                    except FacebookError as err1:
                        # 17–18.11: Facebook иногда отклоняет длинный комментарий с частью 2 (FB 1/1446042, без
                        # объяснений): сначала тот же текст без строки «лайк и „Да“», затем — только «ЧАСТЬ 3» + ссылка
                        # (часть 2 всё равно есть в статье на сайте)
                        if err1.code != 1 or message == link:
                            raise
                        c = None
                        alt = _without_outro(message)
                        if alt != message:
                            try:
                                c = self.graph.comment(target, tok, alt)
                                e["comment_variant"] = "без строки «лайк и Да»"
                            except FacebookError as err2:
                                if err2.code != 1:
                                    raise
                        if c is None:
                            last = message.strip().split("\n")
                            short = "\n".join(last[-2:]) if len(last) >= 2 else link
                            c = self.graph.comment(target, tok, short)
                            e["comment_variant"] = "только «ЧАСТЬ 3» и ссылка"
                    e.update(comment_id=c.get("id"), status="done", error=None)
                    self.r.counts["comments"] += 1
                    self.pacer.ok(self.pacer.owner(pid))
                    self.log_event(slot=key, lang=lang, page=pid, action="comment", id=c.get("id"))
                    self.state.save()
                except FacebookError as err:
                    if err.rate_limited:
                        minutes = self._owner_block(pid)
                        self.page_error(key, name, e, err, f"комментарий (Facebook ограничил частоту — пауза {minutes} мин)")
                        continue
                    self.page_error(key, name, e, err, "комментарий")

    def _newer_release_started(self, dt, now) -> bool:
        for k, s2 in (self.state.data.get("slots") or {}).items():
            if not s2.get("source") or s2.get("status") in ("cancelled", "no_source", "missed", "skipped"):
                continue
            try:
                d2 = datetime.strptime(k[:16], "%Y-%m-%d %H:%M").replace(tzinfo=self.tz)
            except ValueError:
                continue
            if dt < d2 <= now:
                return True
        return False

    def _slow(self, page_id) -> bool:
        return self.pacer.owner(page_id) in self.pacer.per_owner

    def _owner_block(self, page_id) -> int:
        owner = self.pacer.owner(page_id)
        minutes = self.pacer.block(owner, self.now())
        self.state.save()
        self.r.error(f"Facebook ограничил частоту для «{owner}» — его страницы на паузе {minutes} мин, "
                     f"остальные продолжают")
        return minutes

    def _owner_paused(self, key, page_id, now) -> bool:
        owner = self.pacer.owner(page_id)
        until = self.pacer.blocked_until(owner, now)
        if until and owner not in self._told_block:
            self._told_block.add(owner)
            self.r.info(f"{key}: «{owner}» — пауза Facebook до {until.astimezone(self.tz):%H:%M}, потом повторю")
        return bool(until)

    def close_slot(self, dt, final=False):
        key = self.key(dt)
        s = self.state.slots.get(key)
        if not s or not s.get("prepared"):
            return
        current = {p.page_id for p in self.cfg.pages}   # страницы, перенесённые в другую группу, не ждём
        statuses = [e["status"] for L in s["langs"].values() for pid, e in L.get("pages", {}).items() if pid in current]
        expected = sum(len(self.cfg.pages_for(lg)) for lg in LANG_ORDER)
        if len(statuses) == expected and all(x in OK_PAGE for x in statuses):
            s["status"] = "done"
        elif final:
            for L in s["langs"].values():
                for pid, e in L.get("pages", {}).items():
                    if e["status"] not in OK_PAGE + ("failed_final",):
                        what = "комментарий не добавлен" if e["status"] in LIVE_PAGE else "не опубликовано"
                        self.r.error(f"{key} {pid}: {what} ({e.get('error') or e['status']})", final=True)
                        e["status"] = "failed_final"
            s["status"] = "done_with_errors"
        else:
            s["status"] = "in_progress"
        self.state.save()
