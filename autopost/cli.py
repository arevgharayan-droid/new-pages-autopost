"""Команды: check · setup-facebook · private-test · public-test · tick · status · simulate · render-workflow."""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config as config_mod
from .config import LANG_ORDER
from .fb import FacebookError, Graph, redact
from .generate import GenerationError, Generator, words
from .pipeline import AuthProblem, Pacer, Pipeline, Report
from .sources import (SourceError, fetch_post, google_sheet_links, link_key, manual_links, normalize_link, parse_link,
                      pick_source, resolve_share_link, sheet_baseline, source_key)
from .state import State
from .tokens import TokenError, load_tokens, setup_facebook
from .workflow import render_cron_block, write_workflow
from .wp import WordPress, WordPressError


def summary(md: str, file: str | None = None, cfg=None):
    """Пишет отчёт в лог, в сводку GitHub Actions и (по желанию) в файл previews/…"""
    print(md)
    gs = os.environ.get("GITHUB_STEP_SUMMARY")
    if gs:
        with open(gs, "a", encoding="utf-8") as f:
            f.write(md + "\n")
    if file and cfg:
        p = cfg.path(file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(md, encoding="utf-8")


def site_secrets(site: dict) -> tuple:
    """Имена секретов сайта: ключ Make Connector или логин + пароль приложения (по умолчанию — как раньше)."""
    return (site.get("key_secret") or "WP_API_KEY", site.get("user_secret") or "WP_USER",
            site.get("password_secret") or "WP_APP_PASSWORD")


def real_wp(site: dict):
    # site.wp_auth: application_password (пароль приложения WordPress) или make_connector (ключ из секрета)
    key_s, user_s, pass_s = site_secrets(site)
    api_key = os.environ.get(key_s) if site.get("wp_auth") == "make_connector" else None
    return WordPress(site["url"], os.environ.get(user_s, ""), os.environ.get(pass_s, ""), api_key=api_key)


def all_sites(cfg) -> list:
    """Сайты WordPress, на которые пишут группы (без повторов), с группами каждого."""
    out = {}
    for g in cfg.groups():
        site = config_mod.site_for(cfg.settings, g)
        out.setdefault(site["url"], (site, []))[1].append(cfg.group_label(g))
    return list(out.values())


def real_services(cfg):
    import requests
    return Graph(cfg.graph_version), real_wp(cfg.site), requests.Session()


def download(http, url) -> tuple:
    r = http.get(url, timeout=60)
    if r.status_code != 200 or not r.content:
        raise SourceError(f"не удалось скачать изображение (HTTP {r.status_code})")
    return r.content, (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()


def state_of(cfg) -> State:
    return State(cfg.path("state/state.json"))


# ---------------------------------------------------------------- check
def cmd_check(cfg, args) -> int:
    ok, bad = [], []

    def res(good, text):
        (ok if good else bad).append(text)

    probs = config_mod.check_expected_counts(cfg)
    res(not probs, f"Конфигурация: {config_mod.describe(cfg)}; все 34 страницы из Make на месте" if not probs
        else "Конфигурация: " + "; ".join(probs))
    res(True, f"Часы публикаций ({cfg.tz}): {', '.join(cfg.slots)} · расписание "
              f"{'ВКЛЮЧЕНО' if cfg.settings.get('schedule_enabled') else 'выключено'}")
    for name in ("FB_APP_ID", "FB_APP_SECRET"):
        res(bool(os.environ.get(name)), f"Секрет {name} {'задан' if os.environ.get(name) else 'НЕ задан'}")
    for site, labels in all_sites(cfg):
        key_s, user_s, pass_s = site_secrets(site)
        mc = site.get("wp_auth") == "make_connector"
        wp_ok = os.environ.get(key_s) if mc else (os.environ.get(user_s) and os.environ.get(pass_s))
        res(bool(wp_ok), f"WordPress {site['url']} ({', '.join(labels)}): "
            + (f"ключ Make Connector ({key_s})" if mc else f"пароль приложения ({user_s} + {pass_s})")
            + (" задан" if wp_ok else " НЕ задан"))
    has_claude = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
    res(bool(has_claude), "Секрет Claude (CLAUDE_CODE_OAUTH_TOKEN или ANTHROPIC_API_KEY) "
        + ("задан" if has_claude else "НЕ задан"))
    graph, wp, _http = real_services(cfg)
    tokens = {}
    try:
        tokens = load_tokens(cfg)
        res(True, f"Токены Facebook: расшифрованы, страниц: {len(tokens)}")
        renamed, failed = [], []
        for p in cfg.pages:
            tok = tokens.get(p.page_id)
            if not tok:
                failed.append(f"{p.name} — нет токена")
                continue
            try:
                info = graph.get(p.page_id, tok, fields="id,name")
                if info.get("name") != p.name:
                    renamed.append(f"{p.name} → сейчас «{info.get('name')}»")
            except FacebookError as e:
                failed.append(f"{p.name} — {e.short()}")
        res(not failed, f"Facebook: все {len(cfg.pages)} страниц доступны" if not failed
            else "Facebook: проблемы со страницами: " + "; ".join(failed))
        if renamed:
            ok.append("Facebook: названия отличаются от Make (ID совпадают, это не ошибка): " + "; ".join(renamed))
    except TokenError as e:
        res(False, f"Facebook: {e}")
    for site, _labels in all_sites(cfg):
        host = site["url"].split("//")[-1]
        try:
            me = real_wp(site).me()
            caps = me.get("capabilities") or {}
            good = caps.get("publish_posts") or caps.get("administrator") or "administrator" in (me.get("roles") or [])
            res(bool(good), f"WordPress {host}: вход выполнен как «{me.get('name')}»"
                + ("" if good else " — но у пользователя нет права публиковать записи"))
        except WordPressError as e:
            hint = ""
            if "not currently logged in" in str(e) or "не авторизован" in str(e).lower():
                hint = (f" — хостинг, похоже, вырезает заголовок авторизации. Решение: секрет {site_secrets(site)[0]} "
                        "с ключом плагина Make Connector (WordPress → Make → API Key).")
            res(False, f"WordPress {host}: {e}{hint}")
    try:
        used = Generator(cfg).ping()
        res(True, f"Claude: отвечает (способ: {'подписка Max' if used == 'subscription' else used})")
    except GenerationError as e:
        res(False, f"Claude: {e}")
    if tokens:
        st = copy.deepcopy(state_of(cfg).data)
        tmp = State(Path(tempfile.mkdtemp()) / "s.json")
        tmp.data = st
        try:
            src = pick_source(cfg, graph, tokens, tmp, datetime.now(timezone.utc), lambda m: None)
            res(True, "Источник историй: найдена следующая история — " + (src["link"][:100] if src else
                "пока новых нет (опубликуйте историю с фото на любой из страниц или добавьте ссылку в очередь)"))
        except (FacebookError, SourceError) as e:
            res(False, f"Источник историй: {e}")
    md = "## Проверка подключений\n\n" + "\n".join([f"- ✅ {x}" for x in ok] + [f"- ❌ {x}" for x in bad]) + "\n"
    summary(md, "previews/check-latest.md", cfg)
    return 1 if bad else 0


# ---------------------------------------------------------------- setup facebook
def cmd_setup_facebook(cfg, args) -> int:
    graph = Graph(cfg.graph_version)
    try:
        rep = setup_facebook(cfg, graph, os.environ.get("FB_APP_ID", ""), os.environ.get("FB_APP_SECRET", ""),
                             [os.environ.get("FB_USER_TOKEN", ""), os.environ.get("FB_USER_TOKEN_2", ""),
                              os.environ.get("FB_USER_TOKEN_3", "")])
    except (TokenError, FacebookError) as e:
        msg = e.short() if isinstance(e, FacebookError) else str(e)
        summary(f"## Подключение Facebook\n\n❌ {msg}\n\nЧастая причина: токен из Graph API Explorer действует "
                f"около часа — получите новый и сразу запустите этот шаг.\n")
        return 1
    st = state_of(cfg)
    st.ensure_source_cutoff(datetime.now(timezone.utc))
    st.save()
    lines = ["## Подключение Facebook", "", f"✅ Получены токены страниц: **{len(rep['ok'])} из {len(cfg.pages)}**"]
    if len(rep.get("per_user") or []) > 1:
        lines.append("Системные пользователи (FB_USER_TOKEN, FB_USER_TOKEN_2, FB_USER_TOKEN_3): видят страниц — "
                     + ", ".join(str(n) for n in rep["per_user"]))
    lines.append("Срок действия токенов страниц: " + ("бессрочно" if not rep["expires_at"] else
                 datetime.fromtimestamp(rep["expires_at"], timezone.utc).strftime("%Y-%m-%d")))
    if rep["scopes"]:
        lines.append("Разрешения: " + ", ".join(rep["scopes"]))
    need = {"pages_manage_posts", "pages_read_engagement", "pages_manage_engagement", "pages_show_list"}
    if rep["scopes"] and not need.issubset(rep["scopes"]):
        lines.append("❌ Не хватает разрешений: " + ", ".join(sorted(need - set(rep["scopes"]))))
    if rep["missing"]:
        lines.append("❌ Нет доступа к страницам (добавьте их в портфолио Interes World и назначьте системному "
                     "пользователю autopost): "
                     + "; ".join(p.name for p in rep["missing"]))
    if rep["no_rights"]:
        lines.append("❌ Недостаточно прав на страницах: " + "; ".join(f"{p.name} ({', '.join(t)})" for p, t in rep["no_rights"]))
    if rep["extra_pages"]:
        lines.append("ℹ️ Доступны и другие страницы (пока не в config/pages.yaml): "
                     + "; ".join(f"{n or '?'} (page_id {i})" for i, n in rep["extra_pages"]))
    lines.append("\nНовыми историями считаются посты, опубликованные на ваших страницах ПОСЛЕ этого момента "
                 "(старые посты и всё, что публиковал Make, не берутся).")
    lines.append("Секрет FB_USER_TOKEN не удаляйте: когда в портфолио добавятся новые страницы, "
                 "этот шаг можно просто запустить ещё раз.")
    summary("\n".join(lines) + "\n", "previews/facebook-setup.md", cfg)
    return 0 if not rep["missing"] and not rep["no_rights"] else 1


# ---------------------------------------------------------------- private test
def _peek_source(cfg, graph, tokens, link):
    if link:
        link = normalize_link(link, State(Path(tempfile.mkdtemp()) / "s.json"))  # /share/p/… → полная ссылка
        info = parse_link(link, cfg.profile_to_page())
        post = fetch_post(graph, info, tokens, cfg.fallback_source_page_id)
        from .sources import source_key
        from .state import text_fingerprint
        return {"key": source_key(info["page_id"], info["part"]), "page_id": info["page_id"], "text": post["text"],
                "image_url": post["image_url"], "origin": "manual", "link": link,
                "fingerprint": text_fingerprint(post["text"])}
    tmp = State(Path(tempfile.mkdtemp()) / "s.json")
    tmp.data = copy.deepcopy(state_of(cfg).data)
    tmp.data["source_cutoff"] = None  # для теста подойдёт любая недавняя история с фото
    return pick_source(cfg, graph, tokens, tmp, datetime.now(timezone.utc), lambda m: None)


def cmd_private_test(cfg, args, services=None, tokens=None) -> int:
    if services is None:
        tp = cfg.page(args.page or cfg.test_page_id())
        services = real_services(config_mod.group_view(cfg, tp.group) if tp else cfg)
    graph, wp, http = services
    st = state_of(cfg)
    tz = ZoneInfo(cfg.tz)
    tokens = tokens or load_tokens(cfg)
    page = cfg.page(args.page or cfg.test_page_id())
    if not page:
        print("Страница не найдена в config/pages.yaml")
        return 1
    src = _peek_source(cfg, graph, tokens, args.source)
    if not src:
        summary("## Закрытый тест\n\n❌ Нет новой истории. Опубликуйте историю с фото на любой из ваших страниц "
                "или запустите тест со ссылкой на пост.\n")
        return 1
    test_id = datetime.now(tz).strftime("test-%Y%m%d-%H%M%S")
    gen = Generator(cfg, backend=args.backend)
    langs = cfg.active_langs()
    with ThreadPoolExecutor(max_workers=len(langs)) as pool:
        futs = {lang: pool.submit(gen.generate, lang, src["text"]) for lang in langs}
    texts, errors = {}, {}
    for lang, f in futs.items():
        try:
            texts[lang] = f.result().as_dict()
        except GenerationError as e:
            errors[lang] = str(e)
    tdir = cfg.path(f"content/tests/{test_id}")
    tdir.mkdir(parents=True, exist_ok=True)
    for lang, t in texts.items():
        (tdir / f"{lang}.json").write_text(json.dumps(t, ensure_ascii=False, indent=1), encoding="utf-8")
    checks = []
    rec = {"source": src, "page_id": page.page_id, "created": datetime.now(tz).isoformat(timespec="seconds")}
    # WordPress: фото в медиатеку + черновик (не виден посетителям) → проверка → удаление черновика
    lang = page.lang
    if lang in texts:
        draft_id, fb_id = None, None
        tok = tokens.get(page.page_id)
        try:
            data, mime = download(http, src["image_url"])
            media = wp.upload_media(data, f"{test_id}.jpg", mime)
            rec["wp_media_id"], rec["wp_media_url"] = media["id"], media.get("source_url")
            st.data["tests"][test_id] = rec
            st.save()
            draft = wp.create_post(texts[lang]["title"], texts[lang]["article_html"], status="draft")
            draft_id = draft["id"]
            back = wp.get_post(draft_id)
            checks.append(f"✅ WordPress: черновик создан (id {draft_id}, статус «{back.get('status')}»), "
                          "посетителям не виден")
            # Facebook: отложенный пост через 7 дней (виден только администраторам) → проверка → удаление
            ts = int((datetime.now(timezone.utc) + timedelta(days=7)).timestamp())
            res = graph.create_photo_post(page.page_id, tok, data, "story.jpg", texts[lang]["fb_text"],
                                          scheduled_ts=ts, mime=mime)
            fb_id = res.get("id")
            rec["fb_test_photo"] = fb_id
            st.save()
            sched = [i for i in graph.scheduled_posts(page.page_id, tok)
                     if int(i.get("scheduled_publish_time") or 0) == ts]
            if sched:
                rec["fb_test_post"] = sched[0]["id"]
                published = graph.is_published(sched[0]["id"], tok)
                checks.append(f"{'✅' if not published else '❌'} Facebook «{page.name}»: пост с фото стоит в "
                              f"отложенных на {datetime.fromtimestamp(ts, tz):%d.%m %H:%M}, опубликован: "
                              f"{'да' if published else 'нет'}")
            else:
                checks.append(f"❌ Facebook «{page.name}»: фото загружено, но в списке отложенных постов его нет")
        except Exception as e:  # noqa: BLE001 — показываем любую причину
            msg = e.short() if isinstance(e, FacebookError) else redact(e)
            checks.append(f"❌ {type(e).__name__}: {msg}")
        finally:
            if not args.keep:
                if draft_id:
                    try:
                        wp.delete_post(draft_id)
                        checks.append("✅ Черновик WordPress удалён")
                    except Exception as e:  # noqa: BLE001
                        checks.append(f"❌ Не удалось удалить черновик {draft_id}: {redact(e)} — удалите вручную")
                deleted = False
                for obj in (rec.get("fb_test_post"), fb_id):
                    if obj and not deleted:
                        try:
                            graph.delete(obj, tok)
                            deleted = True
                        except Exception:  # noqa: BLE001
                            pass
                if fb_id:
                    rec["fb_test_post_deleted"] = deleted
                    checks.append("✅ Тестовый отложенный пост Facebook удалён" if deleted else
                                  "❌ Не удалось удалить тестовый пост — удалите его в Meta Business Suite → "
                                  "Планировщик (запланирован на неделю вперёд)")
    st.data["tests"][test_id] = rec
    st.save()
    md = [f"## Закрытый тест {test_id}", "", "Ничего не опубликовано публично.", "",
          f"**Источник:** {src['link']}", f"**Фото:** {src['image_url'][:120]}…",
          f"**Тестовая страница:** {page.name} ({page.page_id}, язык {page.lang})", "", *checks, ""]
    for lg in cfg.active_langs():
        names = ", ".join(p.name for p in cfg.pages_for(lg))
        if lg in texts:
            t = texts[lg]
            md += [f"### {lg} → {len(cfg.pages_for(lg))} стр.: {names}", "", f"**Заголовок:** {t['title']}", "",
                   "**Текст для Facebook:**", "", t["fb_text"], "",
                   f"**Статья:** {len(t['article_html'].split())} слов (полностью — content/tests/{test_id}/{lg}.json)"]
            if t["warnings"]:
                md.append("Замечания: " + "; ".join(t["warnings"]))
            md.append("")
        else:
            md += [f"### {lg}: ❌ {errors.get(lg)}", ""]
    summary("\n".join(md), f"previews/{test_id}.md", cfg)
    return 1 if errors or any(c.startswith("❌") for c in checks) else 0


# ---------------------------------------------------------------- public test
def cmd_public_test(cfg, args, services=None, tokens=None) -> int:
    if args.confirm != "PUBLISH":
        print("Для публичного теста введите в поле confirm слово PUBLISH")
        return 1
    graph, wp, http = services or real_services(cfg)
    st = state_of(cfg)
    tests = st.data.get("tests") or {}
    test_id = args.test_id or (sorted(tests)[-1] if tests else None)
    if not test_id or test_id not in tests:
        print("Не найден закрытый тест — сначала запустите «3 · Закрытый тест»")
        return 1
    rec = tests[test_id]
    page = cfg.page(args.page or rec["page_id"])
    tokens = tokens or load_tokens(cfg)
    t = json.loads(cfg.path(f"content/tests/{test_id}/{page.lang}.json").read_text(encoding="utf-8"))
    import html as _html
    img = (f'<figure class="wp-block-image size-full"><img src="{_html.escape(rec.get("wp_media_url") or "")}" '
           f'alt="{_html.escape(t["title"])}"/></figure>\n')
    out = [f"## Публичный тест {test_id}", ""]
    try:
        post = wp.create_post(t["title"], img + t["article_html"], status="publish",
                              featured_media=rec.get("wp_media_id") if cfg.site.get("wp_featured_image") else None)
        link = cfg.site.get("comment_link_format").format(wp_id=post["id"])
        out.append(f"✅ Статья опубликована: {link}")
        data, _mime = download(http, rec.get("wp_media_url") or rec["source"]["image_url"])
        res = graph.create_photo_post(page.page_id, tokens[page.page_id], data, "story.jpg", t["fb_text"])
        target = res.get("post_id") or res["id"]
        out.append(f"✅ Пост опубликован на «{page.name}»: https://www.facebook.com/{target}")
        c = graph.comment(target, tokens[page.page_id], link)
        out.append(f"✅ Первый комментарий со ссылкой добавлен (id {c.get('id')})")
        st.remember_output(target, t["fb_text"])
        st.remember_output(res.get("id"))
        st.mark_source(rec["source"]["key"], status="used", slot=f"public-test {test_id}",
                       link=rec["source"]["link"], fingerprint=rec["source"].get("fingerprint"))
        rec["public"] = {"wp_id": post["id"], "fb_post": target, "comment": c.get("id"), "page": page.page_id}
        st.save()
        code = 0
    except (FacebookError, WordPressError, SourceError) as e:
        out.append(f"❌ {e.short() if isinstance(e, FacebookError) else e}")
        code = 1
    summary("\n".join(out) + "\n", f"previews/{test_id}-public.md", cfg)
    return code


# ---------------------------------------------------------------- tick
def _checkpoint():
    """Промежуточное сохранение состояния в репозиторий (только в GitHub Actions)."""
    if os.environ.get("AUTOPOST_CHECKPOINT") == "1":
        import subprocess
        subprocess.run(["bash", "scripts/commit-state.sh", "автопостинг — промежуточное сохранение"], check=False)


def _group_states(cfg) -> dict:
    return {g: State(config_mod.group_state_path(cfg, g)) for g in cfg.groups()}


def _all_group_states(cfg) -> dict:
    """Все группы, у которых есть файл состояния (и нынешние, и прежние)."""
    d = cfg.path("state/groups")
    out = {f.stem: State(f) for f in sorted(d.glob("*.json"))} if d.exists() else {}
    for g in cfg.groups():
        out.setdefault(g, State(config_mod.group_state_path(cfg, g)))
    return out


def rework_slot(cfg, g: str, key: str, move_site=False, wp_factory=None, force=False) -> str:
    """Подготовленный, но ещё не вышедший выпуск группы готовится заново по нынешней настройке:
    языки, которые добавились в группу, получают тексты, статьи и посты (уже готовые тексты и посты
    остаются); move_site — статьи переносятся на нынешний сайт группы (старые удаляются). Посты Facebook
    не меняются: ссылка на статью — только в первом комментарии, он добавляется после выхода поста."""
    st = State(config_mod.group_state_path(cfg, g))
    s = st.slots.get(key)
    if not s or not s.get("prepared"):
        return "выпуск не подготовлен — нечего переделывать"
    if s.get("status") in ("done", "done_with_errors", "missed", "cancelled"):
        return f"выпуск уже закрыт ({s.get('status')})"
    if not force and datetime.fromisoformat(s["slot_iso"]) <= datetime.now(timezone.utc) + timedelta(minutes=20):
        return "до выхода меньше 20 минут — не трогаю"
    gcfg = config_mod.group_view(cfg, g)
    notes = []
    old_site = s.get("wp_site") or {}
    new_site = {k: gcfg.site[k] for k in Pipeline.SITE_KEYS if gcfg.site.get(k)}
    if move_site and old_site.get("url") != new_site.get("url"):
        old_wp = (wp_factory or real_wp)(old_site) if old_site.get("url") else None
        removed = 0
        for L in (s.get("langs") or {}).values():
            if L.get("wp_id"):
                try:
                    if old_wp:
                        old_wp.delete_post(L["wp_id"])
                        removed += 1
                except WordPressError as e:
                    notes.append(f"старая статья {L['wp_id']} не удалена: {e}")
                for k in ("wp_id", "wp_live", "wp_error"):
                    L.pop(k, None)
        src = s.get("source") or {}
        if old_wp and src.get("wp_media_id"):
            try:
                old_wp.delete_media(src["wp_media_id"])
            except WordPressError:
                pass
        for k in ("wp_media_id", "wp_media_url"):
            src.pop(k, None)
        s["wp_site"] = new_site
        notes.append(f"статьи переносятся: {old_site.get('url')} → {new_site.get('url')} (старых удалено: {removed})")
    added = []
    for lang in gcfg.group_langs(g) if hasattr(gcfg, "group_langs") else []:
        L = s.setdefault("langs", {}).get(lang)
        if not L or not L.get("generated"):
            s["langs"][lang] = {"pages": {}}
            added.append(lang)
    if added:
        notes.append("новые языки: " + ", ".join(added))
    if not notes:
        return "менять нечего"
    s["prepared"] = False
    s["status"] = "preparing"
    st.save()
    return "; ".join(notes) + " — следующий запуск подготовит"


def _unfinished(st: State) -> bool:
    return any(s.get("prepared") and s.get("status") not in ("done", "done_with_errors", "missed", "cancelled")
               for s in st.slots.values())


def _legacy_groups(cfg) -> dict:
    """Прежние группы (их страницы перенесены в другие), у которых остались недописанные выпуски:
    группа → страницы из этих выпусков. Их дописываем (комментарии, закрытие), новых историй не берём."""
    out = {}
    current = set(cfg.groups())
    d = cfg.path("state/groups")
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        g = f.stem
        if g in current:
            continue
        st = State(f)
        if not _unfinished(st):
            continue
        ids = {pid for s in st.slots.values()
               if s.get("prepared") and s.get("status") not in ("done", "done_with_errors", "missed", "cancelled")
               for L in (s.get("langs") or {}).values() for pid in (L.get("pages") or {})}
        out[g] = (st, ids)
    return out


def cmd_tick(cfg, args) -> int:
    """Один запуск по расписанию: каждая группа страниц — своя история из своей очереди queue/<группа>.txt."""
    enabled = bool(cfg.settings.get("schedule_enabled"))
    states = _group_states(cfg)
    legacy = _legacy_groups(cfg)
    views = {g: config_mod.group_view(cfg, g) for g in states}
    for g, (st, ids) in legacy.items():
        states[g] = st
        views[g] = config_mod.group_view(cfg, g, page_ids=ids)
    if not enabled and not any(_unfinished(st) for st in states.values()):
        for st in states.values():
            if st.data.get("enabled_since"):
                st.data["enabled_since"] = None
                st.save()
        summary("## Автопостинг\n\nРасписание выключено (`schedule_enabled: false`).\n")
        return 0
    try:
        tokens = load_tokens(cfg)
    except TokenError as e:
        summary(f"## Автопостинг\n\n❌ {e}\n")
        return 1
    import threading
    lock = threading.Lock()

    def checkpoint():  # сохранения в репозиторий — по одному, группы работают параллельно
        with lock:
            _checkpoint()

    # один темп на все группы; паузы Facebook — по системным пользователям (autopost2 / старый autopost)
    from .tokens import token_origins
    try:
        owners = {pid: _owner_name(cfg, o) for pid, o in token_origins(cfg).items()}
    except Exception:  # noqa: BLE001 — без карты все страницы считаются одним пользователем
        owners = {}
    pacer = Pacer(float(cfg.settings.get("fb_pacing_seconds", 2)), owners=owners,
                  path=cfg.path("state/fb_guard.json"), per_owner=cfg.settings.get("fb_pacing_by_user") or {})

    def run_group(g):
        gcfg = views[g]
        g_graph, g_wp, g_http = real_services(gcfg)  # своё соединение у каждой группы, сайт группы
        try:
            rep = Pipeline(gcfg, states[g], g_graph, g_wp, Generator(gcfg), tokens, http=g_http,
                           checkpoint=checkpoint, wp_factory=real_wp, pacer=pacer).tick()
        except Exception as e:  # noqa: BLE001 — сбой одной группы не останавливает остальные
            rep = Report()
            rep.error(f"группа {cfg.group_label(g)}: непредвиденная ошибка {type(e).__name__}: {e}", final=True)
        return g, rep

    with ThreadPoolExecutor(max_workers=len(states) or 1) as pool:
        results = list(pool.map(run_group, list(states)))
    md, final = ["## Автопостинг по группам", ""], 0
    for g, rep in results:
        final += len(rep.final_errors)
        label = cfg.group_label(g) if g not in legacy else f"{g} (прежняя группа: дописываю уже готовые выпуски)"
        md.append(rep.markdown(title=f"{label} · {len(views[g].pages)} стр.")
                  .replace("## ", "### ", 1))
    summary("\n".join(md), "previews/tick-latest.md", cfg)
    return 1 if final else 0


# ---------------------------------------------------------------- status
def cmd_status(cfg, args) -> int:
    lines = ["## Состояние выпусков по группам", "",
             "| Группа | Выпуск | Статус | История | Готово страниц |", "|---|---|---|---|---|"]
    for g, st in _group_states(cfg).items():
        for key in sorted(st.slots)[-10:]:
            s = st.slots[key]
            pages = [e for L in s.get("langs", {}).values() for e in L.get("pages", {}).values()]
            done = sum(1 for e in pages if e["status"] in ("done", "skipped_source"))
            link = ((s.get("source") or {}).get("link") or "—")[:60]
            lines.append(f"| {cfg.group_label(g)} | {key} | {s.get('status')} | {link} | {done}/{len(pages)} |")
    summary("\n".join(lines) + "\n", "previews/status.md", cfg)
    return 0


# ---------------------------------------------------------------- simulate (сухой прогон)
def cmd_simulate(cfg, args) -> int:
    from .simulate import run_simulation
    ok, md = run_simulation(cfg, days=args.days)
    summary(md, "previews/dry-run.md", cfg)
    return 0 if ok else 1


def cmd_set_schedule(cfg, args) -> int:
    import re
    p = cfg.path("config/settings.yaml")
    text = p.read_text(encoding="utf-8")
    value = "true" if args.mode == "on" else "false"
    new, n = re.subn(r"^schedule_enabled:\s*\w+", f"schedule_enabled: {value}", text, count=1, flags=re.M)
    if n != 1:
        print("В settings.yaml не найдена строка schedule_enabled")
        return 1
    p.write_text(new, encoding="utf-8")
    st = state_of(cfg)
    st.data["enabled_since"] = None  # отсчёт начнётся с первого запуска после включения
    st.ensure_source_cutoff(datetime.now(timezone.utc))
    st.save()
    summary(f"## Расписание\n\nАвтопостинг {'ВКЛЮЧЁН' if value == 'true' else 'выключен'}. "
            f"Часы ({cfg.tz}): {', '.join(cfg.slots)}\n")
    return 0


def cmd_wp_diagnose(cfg) -> int:
    """Пошаговая проверка записи в WordPress на каждом сайте (тестовые объекты сразу удаляются)."""
    out = ["## Диагностика WordPress", ""]
    for site, labels in all_sites(cfg):
        out += ["", f"### {site['url']} ({', '.join(labels)})", ""]
        _wp_diagnose_site(real_wp(site), out)
    summary("\n".join(out) + "\n", "previews/wp-diagnose.md", cfg)
    return 0


def _wp_diagnose_site(wp, out):
    def step(name, fn):
        try:
            r = fn()
            out.append(f"- ✅ {name}: {r}")
            return r
        except Exception as e:  # noqa: BLE001
            out.append(f"- ❌ {name}: {type(e).__name__}: {redact(e)}")
            return None
    step("users/me", lambda: wp.me().get("name"))
    tiny = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")
    h = {"IWC-API-KEY": wp.api_key} if wp.api_key else {}
    def raw_multipart():
        r = wp.http.post(wp.api + "/media", headers=h, auth=wp.auth,
                         files={"file": ("autopost-test.jpg", tiny, "image/jpeg")}, timeout=60)
        return f"HTTP {r.status_code} ct={r.headers.get('Content-Type')} body={r.text[:400]}"
    step("медиа multipart (сырой ответ)", raw_multipart)
    def raw_multipart2():
        r = wp.http.post(wp.api + "/media", headers=h, auth=wp.auth,
                         files={"file": ("autopost-test2.jpg", tiny, "image/jpeg")},
                         data={"title": "autopost test"}, timeout=60)
        return f"HTTP {r.status_code} body={r.text[:400]}"
    step("медиа multipart + title (сырой ответ)", raw_multipart2)
    def find_media():
        r = wp.http.get(wp.api + "/media", headers=h, auth=wp.auth,
                        params={"search": "autopost-test", "per_page": 10, "context": "edit"}, timeout=60)
        items = r.json() if r.status_code == 200 else []
        ids = [m.get("id") for m in items] if isinstance(items, list) else items
        for mid in (ids if isinstance(ids, list) else []):
            try:
                wp.delete_media(mid)
            except Exception:  # noqa: BLE001
                pass
        return f"найдено и удалено: {ids}"
    step("найти и удалить тестовые медиа", find_media)


def cmd_ops(cfg, args, req=None) -> int:
    """Команда из файла ops/request.json (её коммитит Claude) — чтобы не нажимать кнопки вручную."""
    req = req or json.loads(cfg.path("ops/request.json").read_text(encoding="utf-8"))
    cmd, a = req.get("cmd"), req.get("args") or {}
    ns = argparse.Namespace
    if cmd == "batch":
        # несколько команд подряд в одном запуске (без гонок между отдельными запусками)
        rc = 0
        for step in a.get("steps") or []:
            rc = cmd_ops(cfg, args, req=step) or rc
        return rc
    if cmd == "check":
        return cmd_check(cfg, ns())
    if cmd == "setup-facebook":
        return cmd_setup_facebook(cfg, ns())
    if cmd == "private-test":
        return cmd_private_test(cfg, ns(source=a.get("source", ""), page=a.get("page", ""), keep=False,
                                         backend=a.get("backend")))
    if cmd == "public-test":
        return cmd_public_test(cfg, ns(test_id=a.get("test_id", ""), page=a.get("page", ""),
                                        confirm=a.get("confirm", "")))
    if cmd == "set-schedule":
        return cmd_set_schedule(cfg, ns(mode=a.get("mode", "off")))
    if cmd == "status":
        return cmd_status(cfg, ns())
    if cmd == "wp-diagnose":
        return cmd_wp_diagnose(cfg)
    if cmd == "sheet-status":
        return cmd_sheet_status(cfg)
    if cmd == "check-queue":
        return cmd_check_queue(cfg)
    if cmd == "site-check":
        return cmd_site_check(cfg, a.get("url", ""))
    if cmd == "peek-links":
        return cmd_peek_links(cfg, a.get("links") or [])
    if cmd == "preview-format":
        return cmd_preview_format(cfg, a.get("items") or [], a.get("format", "parts"))
    if cmd == "study-pages":
        return cmd_study_pages(cfg, a.get("links") or [], int(a.get("limit", 6)))
    if cmd == "repost":
        if a.get("from_by_group"):          # у каждой группы свой исходный выпуск (например, внеочередные)
            out = [f"## Повтор выпусков → {a.get('to', '')}", ""]
            states = _group_states(cfg)
            for g, fk in a["from_by_group"].items():
                st = states.get(g)
                out.append(f"- {cfg.group_label(g)}: {fk} → "
                           + (repost_slot(cfg, st, g, fk, a.get("to", "")) if st else "нет такой группы"))
            summary("\n".join(out) + "\n", "previews/repost.md", cfg)
            return 0
        return cmd_repost(cfg, a.get("from", ""), a.get("to", ""))
    if cmd == "inspect-posts":
        return cmd_inspect_posts(cfg, a.get("page", ""), a.get("posts") or [])
    if cmd == "resolve-links":
        return cmd_resolve_links(cfg, a.get("links") or [])
    if cmd == "cancel-slot":
        rc = 0
        only = set(a.get("groups") or [])          # пусто — во всех группах
        for g, st in _all_group_states(cfg).items():
            if (not only or g in only) and a.get("slot", "") in st.slots:
                rc |= cmd_cancel_slot(config_mod.group_view(cfg, g), a.get("slot", ""), state=st)
        return rc
    if cmd == "match-pages":
        return cmd_match_pages(cfg, a.get("pages") or [])
    if cmd == "fb-probe":
        return cmd_fb_probe(cfg, a.get("tokens_file", ""), a.get("page", ""))
    if cmd == "tokens-status":
        return cmd_tokens_status(cfg, check=a.get("check", True))
    if cmd == "retry-sources":
        # ссылки, которые не прочитались (например, не было страницы-«читателя»), снова в очередь
        out = ["## Повторить непрочитанные ссылки", ""]
        needle = a.get("error_contains", "нет доступа к странице")
        for g, st in _group_states(cfg).items():
            keys = [k for k, v in st.sources.items() if v.get("status") == "failed" and needle in (v.get("error") or "")]
            for k in keys:
                del st.sources[k]
            if keys:
                st.save()
            out.append(f"- {cfg.group_label(g)}: {len(keys)}")
        summary("\n".join(out) + "\n", "previews/retry-sources.md", cfg)
        return 0
    if cmd == "move-slot":
        out = [f"## Перенос выпуска {a.get('from', '')} → {a.get('to', '')}", ""]
        for g, st in _group_states(cfg).items():
            if a.get("from", "") in st.slots:
                out.append(f"- {cfg.group_label(g)}: {move_slot(cfg, g, a.get('from', ''), a.get('to', ''))}")
        summary("\n".join(out) + "\n", "previews/move-slot.md", cfg)
        return 0
    if cmd == "reopen-pages":
        # страницы, которые были без доступа, когда готовился выпуск, а теперь подключены, — получают его пост
        out = ["## Повторно для новых подключённых страниц", ""]
        tokens = load_tokens(cfg)
        for g, st in _group_states(cfg).items():
            for key in a.get("slots") or []:
                s = st.slots.get(key)
                if not s or not s.get("prepared"):
                    continue
                n = 0
                for L in (s.get("langs") or {}).values():
                    if not L.get("generated"):
                        continue
                    for pid in [pid for pid, e in (L.get("pages") or {}).items()
                                if e.get("status") == "no_access" and tokens.get(pid)]:
                        del L["pages"][pid]
                        n += 1
                if n:
                    if s.get("status") in ("done", "done_with_errors"):
                        s["status"] = "prepared"
                    st.save()
                out.append(f"- {cfg.group_label(g)} {key}: страниц к публикации — {n}")
        summary("\n".join(out) + "\n", "previews/reopen.md", cfg)
        return 0
    if cmd == "fb-diag":
        return cmd_fb_diag(cfg)
    if cmd == "repost-now":
        out = ["## Повтор выпусков — сейчас", ""]
        for g, fk in (a.get("from_by_group") or {}).items():
            out.append(f"- {cfg.group_label(g)}: {repost_now(cfg, g, fk)}")
        summary("\n".join(out) + "\n", "previews/repost.md", cfg)
        return 0
    if cmd == "skip-slot":
        # выпуск по расписанию пропускается (ещё не подготовленный): ссылки из очереди его не займут
        out = [f"## Пропустить выпуск {a.get('slot', '')}", ""]
        tz = ZoneInfo(cfg.tz)
        for g, st in _group_states(cfg).items():
            if a.get("groups") and g not in a["groups"]:
                continue
            key = a.get("slot", "")
            s = st.slots.get(key)
            if s and (s.get("prepared") or s.get("source_key")):
                out.append(f"- {cfg.group_label(g)}: уже подготовлен — не трогаю")
                continue
            dt = datetime.strptime(key, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            st.slots[key] = {"status": "cancelled", "langs": {}, "slot_iso": dt.isoformat(), "note": "пропущен по просьбе"}
            st.save()
            out.append(f"- {cfg.group_label(g)}: пропущен")
        summary("\n".join(out) + "\n", "previews/skip-slot.md", cfg)
        return 0
    if cmd == "start-now":
        # «сейчас»: внеочередной выпуск на текущую минуту — следующий запуск берёт ссылки из очередей и
        # публикует сразу, без отложенных постов (не создаём, чтобы потом удалять: Facebook это не любит)
        out = ["## Внеочередной выпуск", ""]
        for g in cfg.groups():
            if a.get("groups") and g not in a["groups"]:
                continue
            out.append(f"- {cfg.group_label(g)}: {start_now(cfg, g, at=a.get('at', ''))}")
        summary("\n".join(out) + "\n", "previews/start-now.md", cfg)
        return 0
    if cmd == "comment-try":
        return cmd_comment_try(cfg, a.get("group", ""), a.get("slot", ""), a.get("lang", ""), a.get("page", ""))
    if cmd == "raw-post":
        # что Facebook отдаёт по чужому посту (для разбора «нет текста или фото») — только чтение
        graph = Graph(cfg.graph_version)
        tokens = load_tokens(cfg)
        tok = tokens.get(cfg.fallback_source_page_id) or next(iter(tokens.values()))
        out = ["## Сырые данные постов", ""]
        for pid in a.get("posts") or []:
            try:
                d = graph.get(pid, tok, fields="id,message,full_picture,status_type,story,created_time,"
                              "attachments{media_type,type,title,description,media,target,subattachments.limit(3){media,type}}")
                d["message"] = (d.get("message") or "")[:300]
                out.append(f"- {pid}: ```{json.dumps(d, ensure_ascii=False)[:1500]}```")
            except FacebookError as e:
                out.append(f"- {pid}: ❌ {e.short()}")
        summary("\n".join(out) + "\n", "previews/raw-post.md", cfg)
        return 0
    if cmd == "comment-split":
        return cmd_comment_split(cfg, a.get("group", ""), a.get("slot", ""), a.get("lang", ""), a.get("page", ""))
    if cmd == "comment-fix":
        # исправить уже поставленный первый комментарий: полный текст части 2 без строки «лайк и Да» + ссылка
        from .pipeline import _without_outro
        g, key, lang, pid = a.get("group", ""), a.get("slot", ""), a.get("lang", ""), a.get("page", "")
        st = State(config_mod.group_state_path(cfg, g))
        s = st.slots.get(key) or {}
        L = (s.get("langs") or {}).get(lang) or {}
        site = s.get("wp_site") or config_mod.site_for(cfg.settings, g)
        link = (site.get("comment_link_format") or "").format(wp_id=L.get("wp_id"))
        txt = json.loads(cfg.path(f"content/{key[:10]}/{g}/{key[11:13]}{key[14:16]}-{lang}.json")
                         .read_text(encoding="utf-8")).get("comment_text", "")
        try:
            Graph(cfg.graph_version).post(a.get("comment_id", ""), load_tokens(cfg).get(pid),
                                          data={"message": _without_outro(f"{txt}\n{link}")})
            res = "✅ комментарий исправлен"
        except FacebookError as err:
            res = f"❌ {err.short()}"
        summary(f"## Исправление комментария {a.get('comment_id')}\n\n- {res}\n", "previews/comment-fix.md", cfg)
        return 0
    if cmd == "comment-diag":
        return cmd_comment_diag(cfg, a.get("group", ""), a.get("slot", ""), a.get("lang", ""))
    if cmd == "retry-links":
        # повторить ссылки, которые не удалось прочитать (после исправления) — и, если now, выпустить сразу
        out = []
        for it in a.get("items") or []:
            g, link = it.get("group", ""), it.get("link", "")
            out.append(f"- {cfg.group_label(g)}: {allow_repeat(cfg, g, link)}")
            if a.get("now"):
                out.append(f"  - {start_now(cfg, g)}")
        summary("## Повтор ссылок\n\n" + "\n".join(out) + "\n", "previews/retry-links.md", cfg)
        return 0
    if cmd == "allow-repeat":
        # владелица разрешила повтор: пост, уже выходивший в группе, можно взять снова (по ссылке из очереди)
        g, link = a.get("group", ""), a.get("link", "")
        res = allow_repeat(cfg, g, link)
        if a.get("now"):
            res += "\n- " + start_now(cfg, g)
        summary(f"## Повтор разрешён: {cfg.group_label(g)}\n\n- {res}\n", "previews/allow-repeat.md", cfg)
        return 0
    if cmd == "fb-pause":
        # пауза публикаций во всех группах (Facebook ограничил частоту): minutes=0 — снять паузу
        mins = int(a.get("minutes", 60))
        until = (datetime.now(ZoneInfo(cfg.tz)) + timedelta(minutes=mins)).isoformat(timespec="seconds")
        for g, st in _all_group_states(cfg).items():
            if mins > 0:
                st.data["fb_block_until"] = until
                st.data["fb_block_minutes"] = max(mins, int(st.data.get("fb_block_minutes") or 0))
            else:
                st.data.pop("fb_block_until", None)
                st.data.pop("fb_block_minutes", None)
            st.save()
        summary(f"## Пауза публикаций\n\n{'до ' + until if mins > 0 else 'снята'}\n", "previews/fb-pause.md", cfg)
        return 0
    if cmd == "scheduled":
        return cmd_scheduled(cfg)
    if cmd == "redo-slot":
        out = [f"## Заново подготовить выпуск {a.get('slot', '')}", ""]
        for g, st in _group_states(cfg).items():
            if a.get("groups") and g not in a["groups"]:
                continue
            if a.get("slot", "") in st.slots:
                out.append(f"- {cfg.group_label(g)}: {redo_slot(cfg, g, a.get('slot', ''))}")
        summary("\n".join(out) + "\n", "previews/redo.md", cfg)
        return 0
    if cmd == "publish-now":
        out = [f"## Опубликовать сейчас: выпуск {a.get('slot', '')}", ""]
        for g, st in _group_states(cfg).items():
            if a.get("groups") and g not in a["groups"]:
                continue
            if a.get("slot", "") in st.slots:
                out.append(f"- {cfg.group_label(g)}: {publish_now(cfg, g, a.get('slot', ''))}")
        summary("\n".join(out) + "\n", "previews/publish-now.md", cfg)
        return 0
    if cmd == "drop-lang":
        g, key, lang = a.get("group", ""), a.get("slot", ""), a.get("lang", "")
        gcfg = config_mod.group_view(cfg, g)
        res = drop_lang(gcfg, g, key, lang)
        if a.get("rework", True):
            res += "\n- переподготовка: " + rework_slot(cfg, g, key)
        summary(f"## Убрать язык {lang} из выпуска {cfg.group_label(g)} {key}\n\n{res}\n", "previews/drop-lang.md", cfg)
        return 0
    if cmd == "rework-slot":
        out = ["## Переподготовка выпусков", ""]
        for g in a.get("groups") or []:
            for key in a.get("slots") or []:
                out.append(f"- {cfg.group_label(g)} {key}: "
                           + rework_slot(cfg, g, key, move_site=bool(a.get("move_site"))))
        summary("\n".join(out) + "\n", "previews/rework.md", cfg)
        return 0
    if cmd == "cost-test":
        from .costtest import run as cost_test
        return cost_test(cfg, n=int(a.get("n", 10)),
                         models=tuple(a.get("models") or ("claude-sonnet-5-5", "claude-haiku-5-5")),
                         parallel=int(a.get("parallel", 4)))
    print(f"Неизвестная команда: {cmd}")
    return 1


def repost_slot(cfg, st: State, g: str, from_key: str, to_key: str) -> str:
    """Готовит повтор выпуска: новый слот с теми же текстами и ссылкой на ту же статью (без генерации и новой статьи)."""
    import shutil
    old = st.slots.get(from_key)
    if not old or not old.get("prepared"):
        return "нечего повторять (выпуск не готовился)"
    cur = st.slots.get(to_key)
    if cur and (cur.get("source_key") or cur.get("prepared")):
        return f"выпуск {to_key} уже занят — повтор не поставлен"
    tz = ZoneInfo(cfg.tz)
    dt_new = datetime.strptime(to_key, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    dt_old = datetime.strptime(from_key, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    new = {"slot_iso": dt_new.isoformat(), "status": "prepared", "prepared": True, "repost_of": from_key,
           "source_key": old["source_key"], "source": copy.deepcopy(old.get("source") or {}), "langs": {}}
    if old.get("wp_site"):
        new["wp_site"] = old["wp_site"]   # та же статья — тот же сайт
    for lang, L in (old.get("langs") or {}).items():
        if L.get("generated") and L.get("wp_id"):
            src = cfg.path(f"content/{dt_old:%Y-%m-%d}/{g}/{dt_old:%H%M}-{lang}.json")
            dst = cfg.path(f"content/{dt_new:%Y-%m-%d}/{g}/{dt_new:%H%M}-{lang}.json")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            new["langs"][lang] = {"generated": True, "title": L.get("title"), "wp_id": L["wp_id"], "wp_live": True,
                                  "pages": {}}
        else:
            new["langs"][lang] = {"pages": {pid: {"status": "no_access", "attempts": 0}
                                            for pid in (L.get("pages") or {})}, "skipped": "не повторяется"}
    st.slots[to_key] = new
    st.save()
    return "поставлен"


def cmd_repost(cfg, from_key: str, to_key: str) -> int:
    out = [f"## Повтор выпуска {from_key} → {to_key}", ""]
    for g, st in _group_states(cfg).items():
        out.append(f"- {cfg.group_label(g)}: {repost_slot(cfg, st, g, from_key, to_key)}")
    summary("\n".join(out) + "\n", "previews/repost.md", cfg)
    return 0


def cmd_scheduled(cfg) -> int:
    """Отложенные посты на всех подключённых страницах: время выхода и начало текста."""
    graph = Graph(cfg.graph_version)
    tokens = load_tokens(cfg)
    tz = ZoneInfo(cfg.tz)
    out = ["## Отложенные посты на страницах", ""]
    total = 0
    for g in cfg.groups():
        out += [f"### {cfg.group_label(g)}", ""]
        for p in config_mod.group_view(cfg, g).pages:
            tok = tokens.get(p.page_id)
            if not tok:
                out.append(f"- {p.name}: нет доступа")
                continue
            try:
                sp = sorted(graph.scheduled_posts(p.page_id, tok), key=lambda x: int(x.get("scheduled_publish_time") or 0))
            except FacebookError as e:
                out.append(f"- {p.name}: ошибка {e.short()}")
                continue
            total += len(sp)
            times = ", ".join(datetime.fromtimestamp(int(x.get("scheduled_publish_time") or 0), tz).strftime("%d.%m %H:%M")
                              for x in sp) or "нет"
            out.append(f"- {p.name}: {times}")
        out.append("")
    out.insert(2, f"Всего отложенных постов: **{total}**\n")
    summary("\n".join(out) + "\n", "previews/scheduled.md", cfg)
    return 0


def _rename_slot_slugs(cfg, g, s, dt, wp) -> list:
    """Статьи и фото перенесённого выпуска получают адрес по новому времени — иначе выпуск, который потом
    займёт прежнее время, найдёт их по адресу и возьмёт чужие статью и фото."""
    gcfg = config_mod.group_view(cfg, g)
    multi = len({p.lang for p in gcfg.pages}) > 1
    errors = []
    for lang, L in (s.get("langs") or {}).items():
        if L.get("wp_id"):
            slug = f"iw-{dt:%Y%m%d-%H%M}-{g.lower()}" + (f"-{lang.lower()}" if multi else "")
            try:
                wp.update_post(L["wp_id"], slug=slug)
            except WordPressError as err:
                errors.append(f"адрес статьи {lang}: {err}")
    src = s.get("source") or {}
    if src.get("wp_media_id"):
        try:
            wp.update_media(src["wp_media_id"], slug=f"interes-{dt:%Y%m%d-%H%M}-{g.lower()}")
        except WordPressError as err:
            errors.append(f"адрес фото: {err}")
    return errors


def redo_slot(cfg, g: str, key: str, services=None, tokens=None) -> str:
    """Выпуск ошибочно взял фото и статьи другого выпуска (тот же адрес на сайте): отложенные посты удаляются,
    у перенесённого выпуска адреса меняются, этот выпуск заново получает своё фото, статьи и посты (тексты те же)."""
    st = State(config_mod.group_state_path(cfg, g))
    s = st.slots.get(key)
    if not s or not s.get("prepared"):
        return "выпуск не подготовлен"
    tz = ZoneInfo(cfg.tz)
    dt = datetime.fromisoformat(s["slot_iso"]).astimezone(tz)
    gcfg = config_mod.group_view(cfg, g)
    if services is None:
        services = real_services(gcfg)
        if s.get("wp_site"):
            services = (services[0], real_wp(s["wp_site"]), services[2])
    graph, wp, _http = services
    tokens = tokens or load_tokens(cfg)
    errors = []
    for k2, s2 in st.slots.items():          # освобождаем адреса: выпуск, перенесённый из этого времени
        if s2.get("moved_from") == key:
            errors += _rename_slot_slugs(cfg, g, s2, datetime.fromisoformat(s2["slot_iso"]).astimezone(tz), wp)
    removed, errs = _delete_slot_posts(cfg, g, s, dt, graph, tokens)
    errors += errs
    for L in (s.get("langs") or {}).values():
        if L.get("generated"):
            L["pages"] = {}
            for k in ("wp_id", "wp_live", "wp_error"):
                L.pop(k, None)
    for k in ("wp_media_id", "wp_media_url"):
        (s.get("source") or {}).pop(k, None)
    s["prepared"] = False
    s["status"] = "preparing"
    st.save()
    return (f"удалено отложенных постов {removed}" + (f"; ошибки: {'; '.join(errors[:4])}" if errors else "")
            + " — следующий запуск подготовит заново (фото, статьи, посты)")


def _delete_slot_posts(cfg, g, s, dt, graph, tokens) -> tuple:
    ts = int(dt.timestamp())
    removed, errors = 0, []
    for lang, L in (s.get("langs") or {}).items():
        try:
            txt = json.loads(cfg.path(f"content/{dt:%Y-%m-%d}/{g}/{dt:%H%M}-{lang}.json")
                             .read_text(encoding="utf-8"))["fb_text"]
        except (OSError, KeyError, ValueError):
            txt = ""
        for pid, e in (L.get("pages") or {}).items():
            if e.get("status") not in ("scheduled", "sending"):
                continue
            tok = tokens.get(pid)
            if not tok:
                continue
            try:
                gone = False
                for sp in graph.scheduled_posts(pid, tok):
                    if int(sp.get("scheduled_publish_time") or 0) == ts and (
                            not txt or (sp.get("message") or "")[:60] == txt[:60]):
                        graph.delete(sp["id"], tok)
                        gone = True
                if not gone and e.get("post_id"):
                    graph.delete(e["post_id"], tok)
                    gone = True
                removed += gone
            except FacebookError as err:
                errors.append(f"{cfg.page(pid).name if cfg.page(pid) else pid}: {err.short()}")
    return removed, errors


def cmd_match_pages(cfg, wanted: list) -> int:
    """Какие из присланных страниц (профиль или ссылка + название) доступны системному пользователю:
    ищем по id профиля в ссылке страницы, затем по названию."""
    import unicodedata
    graph = Graph(cfg.graph_version)
    token = os.environ.get("FB_USER_TOKEN", "")
    acc = graph.paged("me/accounts", token, fields="id,name,link,tasks", limit=100)
    norm = lambda x: unicodedata.normalize("NFC", (x or "")).strip().casefold()   # noqa: E731
    by_prof, by_name = {}, {}
    for pg in acc:
        m = re.search(r"id=(\d+)", pg.get("link") or "")
        if m:
            by_prof[m.group(1)] = pg
        by_name.setdefault(norm(pg.get("name")), []).append(pg)
    known = {p.page_id for p in cfg.all_pages}
    out, found = ["## Новые страницы: доступ", ""], []
    for w in wanted:
        prof, name = str(w.get("profile_id") or ""), w.get("name", "")
        pg = by_prof.get(prof) if prof else None
        how = "по профилю"
        if not pg:
            cands = by_name.get(norm(name)) or []
            pg = cands[0] if len(cands) == 1 else None
            how = "по названию" if pg else ("несколько с таким названием" if cands else "")
        if pg:
            tasks = set(pg.get("tasks") or [])
            ok = {"CREATE_CONTENT", "MODERATE"} <= tasks or "MANAGE" in tasks
            already = " (уже подключена)" if pg["id"] in known else ""
            out.append(f"- ✅ {name}: page_id {pg['id']} «{pg.get('name')}» ({how}){already}"
                       + ("" if ok else f" — мало прав: {', '.join(sorted(tasks))}"))
            found.append({"name": name, "page_id": pg["id"], "fb_name": pg.get("name"), "profile_id": prof,
                          "lang": w.get("lang"), "rights_ok": ok, "known": pg["id"] in known})
        else:
            out.append(f"- ❌ {name} ({prof or w.get('link', '')}): нет доступа"
                       + (f" — {how}" if how and how != "по профилю" else ""))
    out.insert(2, f"Доступно: **{len(found)} из {len(wanted)}** · всего страниц у autopost: {len(acc)}\n")
    cfg.path("previews").mkdir(exist_ok=True)
    cfg.path("previews/match-pages.json").write_text(json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
    summary("\n".join(out) + "\n", "previews/match-pages.md", cfg)
    return 0


def _owner_name(cfg, origin: str) -> str:
    """Источник токена → имя системного пользователя для пауз и отчётов."""
    if origin == "main":
        return "autopost2"
    from .tokens import extra_token_files
    if origin in extra_token_files(cfg):
        return "autopost"      # старый системный пользователь
    return origin              # autopost2 / autopost3 из основного набора


def cmd_tokens_status(cfg, check=True) -> int:
    """По группам: какая страница через кого публикуется (autopost2 / старый autopost / нет доступа).
    check — проверить каждый токен чтением имени страницы (ничего не публикует)."""
    from .tokens import token_origins
    tokens = load_tokens(cfg)
    origins = token_origins(cfg)
    graph = Graph(cfg.graph_version) if check else None
    out = ["## Доступ к страницам", ""]
    from collections import Counter
    total = Counter()
    for g in cfg.groups():
        rows = []
        for lang in cfg.languages:
            for p in cfg.pages_for(lang):
                if (p.group or p.lang) != g or p.disabled:
                    continue
                o = origins.get(p.page_id)
                who = _owner_name(cfg, o) if o else "нет доступа"
                note = ""
                if o and graph:
                    try:
                        graph.get(p.page_id, tokens[p.page_id], fields="name")
                    except FacebookError as e:
                        who, note = "токен не работает", f" ({e.short()})"
                total[who] += 1
                rows.append(f"  - {p.name} [{lang}] — {who}{note}")
        out.append(f"- **{cfg.group_label(g)}**")
        out += rows
    out += ["", "Итого: " + " · ".join(f"{k}: {v}" for k, v in total.items())]
    summary("\n".join(out) + "\n", "previews/tokens-status.md", cfg)
    return 0


def cmd_fb_probe(cfg, tokens_file: str, page_id: str) -> int:
    """Проверка, публикует ли набор токенов (например, старого autopost): один скрытый отложенный пост
    на +3 дня на одной странице — и сразу удаляется."""
    import requests
    from cryptography.fernet import InvalidToken
    from .tokens import _fernet
    out = ["## Проба публикации", ""]
    try:
        data = json.loads(_fernet(os.environ.get("FB_APP_SECRET", "")).decrypt(cfg.path(tokens_file).read_bytes()))
        toks = data["tokens"]
    except (OSError, InvalidToken, ValueError, KeyError) as e:
        summary(f"## Проба публикации\n\nне удалось прочитать токены: {e}\n", "previews/fb-probe.md", cfg)
        return 1
    tok = toks.get(page_id)
    p = cfg.page(page_id)
    out.append(f"Токенов в наборе: {len(toks)} · страница: {p.name if p else page_id} · токен есть: {bool(tok)}")
    if not tok:
        summary("\n".join(out) + "\n", "previews/fb-probe.md", cfg)
        return 1
    img = None
    for gst in _group_states(cfg).values():
        for s in sorted(gst.slots.values(), key=lambda x: x.get("slot_iso", ""), reverse=True):
            u = (s.get("source") or {}).get("wp_media_url") or (s.get("source") or {}).get("image_url")
            if u:
                try:
                    r = requests.get(u, timeout=30)
                    if r.ok and len(r.content) > 5000:
                        img = r.content
                        break
                except requests.RequestException:
                    pass
        if img:
            break
    graph = Graph(cfg.graph_version)
    ts = int((datetime.now(timezone.utc) + timedelta(days=3)).timestamp())
    try:
        res = graph.create_photo_post(page_id, tok, img, "probe.jpg", "Проверка (будет удалена)", scheduled_ts=ts)
        out.append(f"- ✅ публикация разрешена: отложенный пост создан ({res.get('post_id') or res.get('id')})")
        for oid in (res.get("post_id"), res.get("id")):
            if oid:
                try:
                    graph.delete(oid, tok)
                except FacebookError:
                    pass
        left = [sp for sp in graph.scheduled_posts(page_id, tok) if int(sp.get("scheduled_publish_time") or 0) == ts]
        for sp in left:
            graph.delete(sp["id"], tok)
        out.append("- пробный пост удалён")
    except FacebookError as e:
        out.append(f"- ❌ Facebook отказал: {e.short()}")
    summary("\n".join(out) + "\n", "previews/fb-probe.md", cfg)
    return 0


def cmd_fb_diag(cfg) -> int:
    """Что Facebook говорит о лимитах: заголовки использования (приложение, страница, бизнес), токен autopost,
    состояние приложения. Только чтение, ничего не публикует."""
    import requests
    base = f"https://graph.facebook.com/{cfg.graph_version}"
    tokens = load_tokens(cfg)
    app_id, app_secret = os.environ.get("FB_APP_ID", ""), os.environ.get("FB_APP_SECRET", "")
    app_token = f"{app_id}|{app_secret}"
    user_token = os.environ.get("FB_USER_TOKEN", "")
    out = ["## Диагностика Facebook (только чтение)", ""]

    def show(label, r):
        hdr = {k: v for k, v in r.headers.items() if k.lower() in (
            "x-app-usage", "x-business-use-case-usage", "x-page-usage", "x-ad-account-usage")}
        try:
            body = r.json()
        except ValueError:
            body = r.text[:200]
        if isinstance(body, dict):
            body = {k: v for k, v in body.items() if k not in ("access_token",)}
        out.append(f"- **{label}**: HTTP {r.status_code} · {json.dumps(body, ensure_ascii=False)[:600]}")
        for k, v in hdr.items():
            out.append(f"  - {k}: {v[:600]}")
    pages = [p for p in cfg.pages if tokens.get(p.page_id)][:3]
    for p in pages:
        show(f"страница {p.name} (чтение)", requests.get(f"{base}/{p.page_id}", params={
            "fields": "name,is_published,verification_status", "access_token": tokens[p.page_id]}, timeout=30))
    if user_token:
        show("токен autopost (debug_token)", requests.get(f"{base}/debug_token", params={
            "input_token": user_token, "access_token": app_token}, timeout=30))
        show("autopost /me", requests.get(f"{base}/me", params={"fields": "id,name", "access_token": user_token}, timeout=30))
    show("приложение", requests.get(f"{base}/{app_id}", params={
        "fields": "name,restrictions,app_domains,link", "access_token": app_token}, timeout=30))
    summary("\n".join(out) + "\n", "previews/fb-diag.md", cfg)
    return 0


def cmd_comment_diag(cfg, g: str, key: str, lang: str) -> int:
    """Почему не ставится первый комментарий: проверка ссылки (сайт и разбор ссылки Facebook) — ничего не публикует."""
    import requests
    st = State(config_mod.group_state_path(cfg, g))
    s = st.slots.get(key) or {}
    L = (s.get("langs") or {}).get(lang) or {}
    site = s.get("wp_site") or config_mod.site_for(cfg.settings, g)
    link = (site.get("comment_link_format") or "").format(wp_id=L.get("wp_id"))
    out = [f"## Диагностика комментария {cfg.group_label(g)} {key} {lang}", "", f"- ссылка: {link}"]
    try:
        r = requests.get(link, timeout=30, allow_redirects=True,
                         headers={"User-Agent": "facebookexternalhit/1.1"})
        title = re.search(r"<title>(.*?)</title>", r.text, re.S)
        og = re.findall(r'<meta property="og:(title|image|url)" content="([^"]*)"', r.text)
        out.append(f"- сайт (как робот Facebook): HTTP {r.status_code}, итоговый адрес {r.url}, "
                   f"title «{(title.group(1).strip() if title else '')[:100]}», og: {og[:3]}")
    except requests.RequestException as e:
        out.append(f"- сайт: ошибка {e}")
    tokens = load_tokens(cfg)
    pid = next(iter((L.get("pages") or {})), "")
    tok = tokens.get(pid)
    graph = Graph(cfg.graph_version)
    try:
        res = graph.post("", tok, data={"id": link, "scrape": "true"})
        out.append(f"- Facebook разбор ссылки: {json.dumps(res, ensure_ascii=False)[:500]}")
    except FacebookError as e:
        out.append(f"- Facebook разбор ссылки: ошибка {e.short()}")
    try:
        txt = json.loads(cfg.path(f"content/{key[:10]}/{g}/{key[11:13]}{key[14:16]}-{lang}.json")
                         .read_text(encoding="utf-8")).get("comment_text", "")
        out.append(f"- текст комментария: {len(txt)} знаков; необычные символы: "
                   f"{sorted({c for c in txt if ord(c) > 0x2000 and c not in '—–…«»“”„’‘'})}")
    except (OSError, ValueError) as e:
        out.append(f"- текст комментария: {e}")
    summary("\n".join(out) + "\n", "previews/comment-diag.md", cfg)
    return 0


def cmd_comment_split(cfg, g: str, key: str, lang: str, page_id: str) -> int:
    """Проба на ОДНОЙ странице: часть 2 комментарием без ссылки, ссылка — ответом под ним («ЧАСТЬ 3» + ссылка)."""
    from .pipeline import _without_outro
    st = State(config_mod.group_state_path(cfg, g))
    s = st.slots.get(key) or {}
    L = (s.get("langs") or {}).get(lang) or {}
    e = (L.get("pages") or {}).get(page_id) or {}
    site = s.get("wp_site") or config_mod.site_for(cfg.settings, g)
    link = (site.get("comment_link_format") or "").format(wp_id=L.get("wp_id"))
    txt = json.loads(cfg.path(f"content/{key[:10]}/{g}/{key[11:13]}{key[14:16]}-{lang}.json")
                     .read_text(encoding="utf-8")).get("comment_text", "")
    paras = txt.split("\n\n")
    body, part3 = "\n\n".join(paras[:-1]), paras[-1]
    tok = load_tokens(cfg).get(page_id)
    graph = Graph(cfg.graph_version)
    out = [f"## Проба: часть 2 без ссылки + ссылка ответом ({page_id})", ""]
    try:
        c = graph.comment(e["post_id"], tok, body)
        out.append(f"- ✅ часть 2 без ссылки: {c.get('id')}")
        try:
            r = graph.comment(c["id"], tok, f"{part3}\n{link}")
            out.append(f"- ✅ ответ со ссылкой: {r.get('id')}")
        except FacebookError as err:
            out.append(f"- ❌ ответ со ссылкой: {err.short()}")
    except FacebookError as err:
        out.append(f"- ❌ часть 2 без ссылки: {err.short()}")
        try:
            c = graph.comment(e["post_id"], tok, _without_outro(f"{body}\n\n{part3}\n{link}"))
            out.append(f"- ✅ (для сравнения) без строки «лайк»: {c.get('id')}")
        except FacebookError as err2:
            out.append(f"- ❌ (для сравнения) без строки «лайк»: {err2.short()}")
    summary("\n".join(out) + "\n", "previews/comment-split.md", cfg)
    return 0


def cmd_comment_try(cfg, g: str, key: str, lang: str, page_id: str) -> int:
    """Поиск причины отказа Facebook в первом комментарии — на ОДНОЙ странице, варианты по очереди до первого
    успеха: 1) обычный комментарий (часть 2 + ссылка); 2) половина текста + ссылка; 3) только «ЧАСТЬ 3» + ссылка."""
    st = State(config_mod.group_state_path(cfg, g))
    s = st.slots.get(key) or {}
    L = (s.get("langs") or {}).get(lang) or {}
    e = (L.get("pages") or {}).get(page_id) or {}
    site = s.get("wp_site") or config_mod.site_for(cfg.settings, g)
    link = (site.get("comment_link_format") or "").format(wp_id=L.get("wp_id"))
    txt = json.loads(cfg.path(f"content/{key[:10]}/{g}/{key[11:13]}{key[14:16]}-{lang}.json")
                     .read_text(encoding="utf-8")).get("comment_text", "")
    tok = load_tokens(cfg).get(page_id)
    graph = Graph(cfg.graph_version)
    paras = txt.split("\n\n")
    half = "\n\n".join(paras[: max(2, len(paras) // 2)])
    no_outro = "\n\n".join(paras[:-2] + paras[-1:]) if len(paras) > 2 else txt
    variants = [("без строки «лайк и „Да“»", f"{no_outro}\n{link}"), ("половина", f"{half}…\n{link}"),
                ("только ссылка", f"{paras[-1] if paras else ''}\n{link}")]
    out = [f"## Проба комментария: {page_id} ({cfg.group_label(g)} {key} {lang})", "",
           f"- пост: {e.get('post_id')} · фото: {e.get('photo_id')} · текст {len(txt)} знаков"]
    for target in [x for x in (e.get("post_id"), e.get("photo_id")) if x][:1]:
        for name, msg in variants:
            try:
                c = graph.comment(target, tok, msg)
                out.append(f"- ✅ {name}: комментарий поставлен ({c.get('id')})")
                break
            except FacebookError as err:
                out.append(f"- ❌ {name}: {err.short()} · trace {err.fbtrace}")
    summary("\n".join(out) + "\n", "previews/comment-try.md", cfg)
    return 0


def allow_repeat(cfg, g: str, link: str) -> str:
    """Снимает запрет повтора для поста, на который ведёт ссылка: прежняя запись уходит в историю
    (status used_before), и ссылка из очереди снова может быть взята."""
    from .sources import link_key
    st = State(config_mod.group_state_path(cfg, g))
    lk = (st.sources.get(link_key(link)) or {}).get("resolved", "")
    m = re.search(r"story_fbid=(\d+)&id=(\d+)", lk or link)
    keys = [k for k, v in st.sources.items()
            if k.startswith("post:") and ((m and k.endswith("_" + m.group(1))) or v.get("link") == link)]
    if not keys:
        return f"в истории группы нет поста по ссылке {link} — повтор и так возможен"
    done = []
    for k in keys:
        rec = st.sources.pop(k)
        rec["status"] = "used_before"
        st.sources[f"{k}#{rec.get('slot', '')}"] = rec
        done.append(f"{k} (выходил {rec.get('slot', '?')})")
    st.save()
    return "повтор разрешён: " + ", ".join(done)


def start_now(cfg, g: str, now=None, at: str = "") -> str:
    """Внеочередной выпуск: сейчас или к заданному времени (at — «YYYY-MM-DD HH:MM», вне сетки расписания)."""
    st = State(config_mod.group_state_path(cfg, g))
    tz = ZoneInfo(cfg.tz)
    if at:
        dt = datetime.strptime(at, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        if Pipeline.key(dt) in st.slots:
            return f"выпуск {at} уже есть"
        st.slots[at] = {"status": "new", "langs": {}, "slot_iso": dt.isoformat(), "adhoc": True}
        st.save()
        return f"выпуск {at} — возьмёт ссылку из очереди и выйдет в {at[-5:]}"
    dt = (now or datetime.now(tz)).astimezone(tz).replace(second=0, microsecond=0)
    while Pipeline.key(dt) in st.slots:
        dt -= timedelta(minutes=1)
    st.slots[Pipeline.key(dt)] = {"status": "new", "langs": {}, "slot_iso": dt.isoformat(), "adhoc": True}
    st.save()
    return f"выпуск {Pipeline.key(dt)} — следующий запуск возьмёт ссылку из очереди и опубликует сразу"


def repost_now(cfg, g: str, from_key: str, now=None) -> str:
    """Повтор уже подготовленного выпуска прямо сейчас (те же тексты и статьи); языки, которых в нём не было,
    но которые теперь есть в группе, получают тексты и статьи."""
    tz = ZoneInfo(cfg.tz)
    path = config_mod.group_state_path(cfg, g)
    st = State(path)
    dt = (now or datetime.now(tz)).astimezone(tz).replace(second=0, microsecond=0)
    while Pipeline.key(dt) in st.slots:
        dt -= timedelta(minutes=1)
    to_key = Pipeline.key(dt)
    r = repost_slot(cfg, st, g, from_key, to_key)
    if r != "поставлен":
        return r
    st = State(path)
    st.slots[to_key]["adhoc"] = True
    st.save()
    r2 = rework_slot(cfg, g, to_key, force=True)
    return f"{from_key} → {to_key}: поставлен" + ("" if r2 == "менять нечего" else f"; {r2}")


def move_slot(cfg, g: str, from_key: str, to_key: str, services=None, tokens=None, pace=None) -> str:
    """Подготовленный, ещё не вышедший выпуск переносится на другое (будущее) время: отложенные посты
    получают новое время публикации (без удаления), статьи — новую дату и адрес, тексты — новое место."""
    st = State(config_mod.group_state_path(cfg, g))
    old = st.slots.get(from_key)
    if not old or not old.get("prepared"):
        return "выпуск не подготовлен"
    if old.get("status") in ("done", "done_with_errors", "missed", "cancelled"):
        return f"выпуск уже закрыт ({old.get('status')})"
    tz = ZoneInfo(cfg.tz)
    dt_old = datetime.fromisoformat(old["slot_iso"]).astimezone(tz)
    dt_new = datetime.strptime(to_key, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    now = datetime.now(tz)
    if dt_old <= now + timedelta(minutes=12):
        return "до выхода меньше 12 минут — пусть выходит как есть"
    if dt_new <= now + timedelta(minutes=20):
        return "новое время слишком близко — нужно хотя бы 20 минут"
    cur = st.slots.get(to_key)
    if cur and (cur.get("prepared") or cur.get("source_key")):
        return f"выпуск {to_key} уже занят"
    if services is None:
        services = real_services(config_mod.group_view(cfg, g))
        if old.get("wp_site"):
            services = (services[0], real_wp(old["wp_site"]), services[2])
    graph, wp, _http = services
    tokens = tokens or load_tokens(cfg)
    pacer = pace or Pacer(float(cfg.settings.get("fb_pacing_seconds", 2)))
    ts = int(dt_new.timestamp())
    moved, redo, errors = 0, 0, []
    new = copy.deepcopy(old)
    new.update({"slot_iso": dt_new.isoformat(), "status": "prepared", "moved_from": from_key})
    regular = dt_new.strftime("%H:%M") in [str(x) for x in cfg.slots]
    if regular:
        new.pop("adhoc", None)
    else:
        new["adhoc"] = True
    for lang, L in (new.get("langs") or {}).items():
        if not L.get("generated"):
            continue
        for pid, e in list((L.get("pages") or {}).items()):
            if e.get("status") == "no_access":
                del L["pages"][pid]          # вдруг к новому времени страница уже подключена
                continue
            if e.get("status") != "scheduled":
                continue
            tok = tokens.get(pid)
            try:
                ids = [e["post_id"]] if e.get("post_id") else [
                    sp["id"] for sp in graph.scheduled_posts(pid, tok)
                    if int(sp.get("scheduled_publish_time") or 0) == int(dt_old.timestamp())]
                if not ids:
                    raise KeyError("отложенный пост не найден")
                for post_id in ids:
                    pacer.wait()
                    graph.reschedule_post(post_id, tok, ts)
                moved += 1
            except (FacebookError, AssertionError, KeyError) as err:
                if moved == 0 and redo == 0 and isinstance(err, FacebookError):
                    # Facebook вообще не даёт переносить время — ничего не трогаем (удалять и ставить заново
                    # десятки постов подряд — то, что вызвало блокировку)
                    return (f"Facebook не дал перенести время отложенного поста ({err.short()}) — "
                            f"выпуск оставлен на {from_key[-5:]}")
                # не переносится — удаляем, следующий запуск поставит заново на новое время
                if e.get("post_id"):
                    try:
                        graph.delete(e["post_id"], tok)
                    except FacebookError:
                        pass
                del L["pages"][pid]
                redo += 1
                errors.append(f"{cfg.page(pid).name if cfg.page(pid) else pid}: "
                              f"{err.short() if isinstance(err, FacebookError) else err}")
        src = cfg.path(f"content/{dt_old:%Y-%m-%d}/{g}/{dt_old:%H%M}-{lang}.json")
        dst = cfg.path(f"content/{dt_new:%Y-%m-%d}/{g}/{dt_new:%H%M}-{lang}.json")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.exists():
            shutil.move(src, dst)
        if L.get("wp_id"):
            try:
                wp.update_post(L["wp_id"], status="future",
                               date_gmt=dt_new.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))
            except WordPressError as err:
                errors.append(f"статья {lang}: {err}")
    errors += _rename_slot_slugs(cfg, g, new, dt_new, wp)
    st.slots[to_key] = new
    del st.slots[from_key]
    st.save()
    return (f"{from_key} → {to_key}: перенесено отложенных постов {moved}"
            + (f", поставятся заново {redo}" if redo else "")
            + (f"; замечания: {'; '.join(errors[:3])}" if errors else ""))


def publish_now(cfg, g: str, from_key: str, services=None, tokens=None, now=None) -> str:
    """Подготовленный выпуск выходит сейчас, а не в своё время: отложенные посты Facebook этого выпуска
    удаляются, тот же выпуск (тексты, статьи) ставится на текущую минуту и публикуется следующим запуском,
    статьи на сайте открываются сразу. Слот from_key освобождается — в него можно поставить другие ссылки."""
    st = State(config_mod.group_state_path(cfg, g))
    old = st.slots.get(from_key)
    if not old or not old.get("prepared"):
        return "выпуск не подготовлен"
    if old.get("status") in ("done", "done_with_errors", "missed", "cancelled"):
        return f"выпуск уже закрыт ({old.get('status')})"
    tz = ZoneInfo(cfg.tz)
    now = (now or datetime.now(tz)).astimezone(tz)
    dt_old = datetime.fromisoformat(old["slot_iso"]).astimezone(tz)
    if dt_old <= now + timedelta(minutes=5):
        return "до выхода меньше 5 минут — пусть выходит как есть"
    dt_new = now.replace(second=0, microsecond=0)
    while Pipeline.key(dt_new) in st.slots:
        dt_new -= timedelta(minutes=1)
    to_key = Pipeline.key(dt_new)
    gcfg = config_mod.group_view(cfg, g)
    if services is None:
        services = real_services(gcfg)
        if old.get("wp_site"):
            services = (services[0], real_wp(old["wp_site"]), services[2])
    graph, wp, _http = services
    tokens = tokens or load_tokens(cfg)
    ts = int(dt_old.timestamp())
    removed, errors = 0, []
    for lang, L in (old.get("langs") or {}).items():
        try:
            txt = json.loads(cfg.path(f"content/{dt_old:%Y-%m-%d}/{g}/{dt_old:%H%M}-{lang}.json")
                             .read_text(encoding="utf-8"))["fb_text"]
        except (OSError, KeyError, ValueError):
            txt = ""
        for pid, e in (L.get("pages") or {}).items():
            if e.get("status") not in ("scheduled", "sending"):
                continue
            tok = tokens.get(pid)
            if not tok:
                continue
            try:
                gone = False
                for sp in graph.scheduled_posts(pid, tok):
                    if int(sp.get("scheduled_publish_time") or 0) == ts and (
                            not txt or (sp.get("message") or "")[:60] == txt[:60]):
                        graph.delete(sp["id"], tok)
                        gone = True
                if not gone and e.get("post_id"):
                    graph.delete(e["post_id"], tok)
                    gone = True
                removed += gone
            except FacebookError as err:
                errors.append(f"{cfg.page(pid).name if cfg.page(pid) else pid}: {err.short()}")
    new = copy.deepcopy(old)
    new.update({"slot_iso": dt_new.isoformat(), "status": "prepared", "prepared": True, "adhoc": True,
                "moved_from": from_key})
    for lang, L in (new.get("langs") or {}).items():
        if L.get("generated"):
            L["pages"] = {}
            src = cfg.path(f"content/{dt_old:%Y-%m-%d}/{g}/{dt_old:%H%M}-{lang}.json")
            dst = cfg.path(f"content/{dt_new:%Y-%m-%d}/{g}/{dt_new:%H%M}-{lang}.json")
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.exists():
                shutil.move(src, dst)   # перенос, а не копия: иначе старый слот «восстановит» историю из файла
            if L.get("wp_id"):
                try:
                    wp.update_post(L["wp_id"], status="publish",
                                   date_gmt=dt_new.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))
                    L["wp_live"] = True
                except WordPressError as err:
                    errors.append(f"статья {lang} ({L['wp_id']}): {err}")
    errors += _rename_slot_slugs(cfg, g, new, dt_new, wp)
    st.slots[to_key] = new
    del st.slots[from_key]
    st.save()
    return (f"→ {to_key}: удалено отложенных постов {removed}"
            + (f"; ошибки: {'; '.join(errors[:4])}" if errors else "") + " — публикуется следующим запуском")


def cmd_site_check(cfg, url: str) -> int:
    """Проверка нового сайта WordPress без ключей: отвечает ли REST API, какие плагины-подключения есть."""
    import requests
    base = (url or cfg.settings["site"]["url"]).rstrip("/")
    out = [f"## Проверка сайта {base}", ""]
    ua = {"User-Agent": "interes-autopost/1.0"}
    for path in ("/wp-json/", "/wp-json/wp/v2/users/me", "/wp-json/wp/v2/posts?per_page=1", "/?rest_route=/"):
        try:
            r = requests.get(base + path, headers=ua, timeout=30)
            ct = r.headers.get("Content-Type", "")
            extra = ""
            if path == "/wp-json/" and "json" in ct:
                try:
                    d = r.json()
                    extra = f" · сайт «{d.get('name')}» · namespaces: {', '.join(d.get('namespaces', []))[:600]}"
                    extra += f" · auth: {', '.join((d.get('authentication') or {}).keys())}"
                except ValueError:
                    pass
            out.append(f"- {path}: HTTP {r.status_code} · {ct[:40]}{extra} · {r.text[:120]!r}")
        except requests.RequestException as e:
            out.append(f"- {path}: ошибка {e}")
    # Доходит ли заголовок авторизации до WordPress: заведомо несуществующий логин должен дать invalid_username,
    # а rest_not_logged_in значит, что хостинг вырезает заголовок. Затем — код ответа с секретами (без самих секретов).
    probes = [("пробный несуществующий логин", ("autopost-probe-no-such-user", "abcd efgh ijkl mnop qrst uvwx"))]
    if os.environ.get("WP_USER") and os.environ.get("WP_APP_PASSWORD"):
        probes.append(("секреты WP_USER + WP_APP_PASSWORD", (os.environ["WP_USER"], os.environ["WP_APP_PASSWORD"])))
    for label, auth in probes:
        try:
            r = requests.get(base + "/wp-json/wp/v2/users/me", headers=ua, auth=auth, timeout=30)
            try:
                d = r.json()
                code = d.get("code") or f"вход выполнен, пользователь «{d.get('name')}»"
            except ValueError:
                code = r.text[:80]
            out.append(f"- авторизация ({label}): HTTP {r.status_code} · {code}")
        except requests.RequestException as e:
            out.append(f"- авторизация ({label}): ошибка {e}")
    out += ["", "Расшифровка: invalid_username — такого логина на сайте нет; incorrect_password — пароль приложения "
            "неверный; rest_not_logged_in — заголовок авторизации не доходит до WordPress."]
    summary("\n".join(out) + "\n", "previews/site-check.md", cfg)
    return 0


def cmd_peek_links(cfg, links: list) -> int:
    """Показывает начало текста по ссылкам (ничего не ставит в очередь и не публикует)."""
    from .generate import detect_language
    graph = Graph(cfg.graph_version)
    tokens = load_tokens(cfg)
    st = State(Path(tempfile.mkdtemp()) / "s.json")
    out = ["## Ссылки: что внутри", ""]
    for i, link in enumerate(links, 1):
        try:
            info = parse_link(normalize_link(link, st, graph, tokens, cfg.fallback_source_page_id),
                              cfg.profile_to_page())
            post = fetch_post(graph, info, tokens, cfg.fallback_source_page_id)
            first = " ".join(post["text"].split())[:150]
            out.append(f"{i}. ({detect_language(post['text']) or '?'}, {len(post['text'])} знаков) «{first}…»")
        except SourceError as e:
            out.append(f"{i}. ❌ {e}")
    summary("\n".join(out) + "\n", "previews/peek-links.md", cfg)
    return 0


def cmd_preview_format(cfg, items: list, fmt: str = "parts") -> int:
    """Пример текстов в заданном формате по реальной ссылке — без публикации (для согласования с владелицей).
    items: [{"link": "...", "lang": "RU"}, …]. Результат: previews/format-preview.md"""
    import copy as _copy
    graph = Graph(cfg.graph_version)
    tokens = load_tokens(cfg)
    st = State(Path(tempfile.mkdtemp()) / "s.json")
    pcfg = _copy.copy(cfg)
    pcfg.settings = _copy.deepcopy(cfg.settings)
    pcfg.settings["post_format"] = fmt
    gen = Generator(pcfg)
    out = [f"## Пример формата «{fmt}» (ничего не опубликовано)", ""]
    for it in items:
        lang, link = it.get("lang", "EN"), it.get("link", "")
        out.append(f"# {cfg.lang_name(lang)} — источник {link}")
        try:
            info = parse_link(normalize_link(link, st, graph, tokens, cfg.fallback_source_page_id), cfg.profile_to_page())
            post = fetch_post(graph, info, tokens, cfg.fallback_source_page_id)
            g = gen.generate(lang, post["text"])
        except (SourceError, GenerationError) as e:
            out.append(f"❌ {e}\n")
            continue
        link_demo = "https://hetaqrqir.com/?p=123"
        out += ["", "### ПОСТ (подпись к фото)", "", "```", g.fb_text, "```", "",
                "### ПЕРВЫЙ КОММЕНТАРИЙ", "", "```", (g.comment_text + "\n" + link_demo).strip(), "```", "",
                f"### СТАТЬЯ НА САЙТЕ: «{g.title}» — {words(g.article_html)} слов", "",
                g.article_html, "", f"Замечания проверки: {', '.join(g.warnings) or 'нет'} · знаков в посте: "
                f"{len(g.fb_text)}, в комментарии: {len(g.comment_text)}", ""]
    if gen.log:
        out += ["", "Журнал генерации: " + " | ".join(gen.log)]
    summary("\n".join(out) + "\n", "previews/format-preview.md", cfg)
    return 0


def _study_candidates(link: str) -> list:
    """Варианты ID / имени страницы по ссылке: profile.php?id=…, facebook.com/<имя>, facebook.com/share/…"""
    from .sources import resolve_share_link
    out = []
    m = re.search(r"[?&]id=(\d+)", link)
    if m:
        out.append(m.group(1))
    if "/share/" in link:
        info = resolve_share_link(link)
        for k in ("page_id", "id"):
            if info.get(k):
                out.append(info[k])
        u = info.get("url", "")
        m = re.search(r"[?&]id=(\d+)", u)
        if m:
            out.append(m.group(1))
        m = re.match(r"https?://(?:www\.|m\.)?facebook\.com/([A-Za-z0-9.\-_]+)/?(?:$|\?)", u)
        if m and m.group(1) not in ("profile.php", "share", "permalink.php", "story.php"):
            out.append(m.group(1))
    m = re.match(r"https?://(?:www\.|m\.)?facebook\.com/([A-Za-z0-9.\-_]+)/?(?:$|\?)", link.strip())
    if m and m.group(1) not in ("profile.php", "share", "permalink.php", "story.php"):
        out.append(m.group(1))
    return list(dict.fromkeys(out))


def cmd_study_pages(cfg, links: list, limit: int = 6) -> int:
    """Образцы чужих страниц: последние посты (полный текст, фото) и первые комментарии под ними.
    Только чтение — ничего не публикует. Результат: previews/study-pages.md и previews/study-pages.json."""
    graph = Graph(cfg.graph_version)
    tokens = load_tokens(cfg)
    order = [tokens.get(cfg.fallback_source_page_id)] + list(tokens.values())
    toks = [t for t in dict.fromkeys(order) if t][:6]
    out, dump = ["## Образцы страниц: посты и комментарии", ""], []
    fields = ("id,message,full_picture,created_time,permalink_url,attachments{media_type,type,url,title},"
              "comments.order(chronological).limit(4){message,from,created_time,attachment{type,url,title}}")
    for link in links:
        cands = _study_candidates(link)
        page, err = None, []
        for c in cands:
            for tok in toks:
                try:
                    page = graph.get(c, tok, fields="id,name,link,category,followers_count,about")
                    page["_tok"] = tok
                    break
                except FacebookError as e:
                    err.append(f"{c}: {e.short()}")
            if page:
                break
        out.append(f"### {link}")
        if not page:
            out.append(f"❌ страница не найдена ({'; '.join(err[-3:]) or 'нет вариантов ID'})\n")
            continue
        tok = page.pop("_tok")
        out.append(f"**{page.get('name')}** (id {page.get('id')}, {page.get('category', '')}, "
                   f"подписчиков: {page.get('followers_count', '?')})\n")
        posts = []
        for edge in ("posts", "published_posts", "feed"):
            try:
                posts = graph.get(f"{page['id']}/{edge}", tok, fields=fields, limit=limit).get("data", [])
                if posts:
                    break
            except FacebookError as e:
                err.append(f"{edge}: {e.short()}")
        if not posts:
            out.append(f"❌ посты не читаются ({'; '.join(err[-3:])})\n")
            continue
        for p in posts:
            msg = p.get("message") or ""
            att = ((p.get("attachments") or {}).get("data") or [{}])[0]
            out.append(f"---\n**{p.get('created_time', '')[:16]}** · {len(msg)} знаков · "
                       f"вложение: {att.get('media_type') or att.get('type') or '—'} · {p.get('permalink_url', '')}\n")
            out.append("```\n" + msg + "\n```")
            for cm in ((p.get("comments") or {}).get("data") or []):
                a2 = cm.get("attachment") or {}
                who = (cm.get("from") or {}).get("name", "?")
                out.append(f"- 💬 {who}: «{cm.get('message', '')}»" + (f" [{a2.get('type')}: {a2.get('url', '')}]" if a2 else ""))
            out.append("")
        dump.append({"link": link, "page": page, "posts": posts})
    cfg.path("previews").mkdir(exist_ok=True)
    cfg.path("previews/study-pages.json").write_text(json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")
    summary("\n".join(out) + "\n", "previews/study-pages.md", cfg)
    return 0


def cmd_inspect_posts(cfg, page_id: str, posts: list) -> int:
    """Диагностика видимости: режим приложения, лента страницы, поля наших постов. Ничего не меняет."""
    import requests
    graph = Graph(cfg.graph_version)
    tokens = load_tokens(cfg)
    tok = tokens.get(page_id)
    out = ["## Проверка видимости постов", ""]
    app_id, secret = os.environ.get("FB_APP_ID", ""), os.environ.get("FB_APP_SECRET", "")
    base = f"https://graph.facebook.com/{cfg.graph_version}"
    try:
        r = requests.get(f"{base}/{app_id}", params={"access_token": f"{app_id}|{secret}",
                         "fields": "name,link,category,company,restrictions"}, timeout=30)
        out.append(f"Приложение: {redact(r.text[:400])}")
        r = requests.get(f"{base}/{app_id}/roles", params={"access_token": f"{app_id}|{secret}"}, timeout=30)
        out.append(f"Роли приложения: {redact(r.text[:600])}")
    except Exception as e:  # noqa: BLE001
        out.append(f"Приложение: ошибка {e}")
    for pid in posts:
        try:
            info = graph.get(pid, tok, fields="id,created_time,is_published,is_hidden,timeline_visibility,"
                                               "status_type,privacy,permalink_url,is_eligible_for_promotion")
            out.append(f"- пост {pid}: {redact(json.dumps(info, ensure_ascii=False))[:500]}")
        except FacebookError as e:
            out.append(f"- пост {pid}: ошибка {e.short()}")
    for edge in ("published_posts", "feed"):
        try:
            data = graph.get(f"{page_id}/{edge}", tok, fields="id,created_time,message", limit=6).get("data", [])
            out.append(f"\n{edge}: " + "; ".join(f"{d['id']} {d.get('created_time', '')[:16]} "
                                                f"«{(d.get('message') or '')[:40]}»" for d in data))
        except FacebookError as e:
            out.append(f"{edge}: ошибка {e.short()}")
    summary("\n".join(out) + "\n", "previews/inspect-posts.md", cfg)
    return 0


def cmd_resolve_links(cfg, links: list) -> int:
    """Раскрывает короткие ссылки facebook.com/share/… — без браузера."""
    p2p = cfg.profile_to_page()
    names = {p.page_id: p.name for p in cfg.all_pages}
    out = ["## Раскрытие ссылок", ""]
    for link in links:
        info = resolve_share_link(link)
        pid = p2p.get(info.get("id", ""), info.get("id", ""))
        out.append(f"- {link} → url={info.get('url', '')[:150]} | story_fbid={info.get('story_fbid', '')} "
                   f"| id={info.get('id', '')} ({names.get(pid, '?')}) | post_id={info.get('post_id', '')} "
                   f"| title={info.get('title', '')}")
    summary("\n".join(out) + "\n", "previews/resolve-links.md", cfg)
    return 0


def drop_lang(gcfg, g: str, key: str, lang: str, services=None, tokens=None) -> str:
    """Убирает один язык из ещё не вышедшего выпуска группы: удаляет его отложенные посты Facebook и статью,
    язык переносится в s['dropped_langs'] (для истории). Остальные языки выпуска не трогаются."""
    st = State(config_mod.group_state_path(gcfg, g))
    s = st.slots.get(key)
    if not s or lang not in (s.get("langs") or {}):
        return f"- в выпуске {key} нет языка {lang}"
    if s.get("status") in ("done", "done_with_errors", "missed", "cancelled"):
        return f"- выпуск уже закрыт ({s.get('status')})"
    if services is None:
        services = real_services(gcfg)
        if s.get("wp_site"):
            services = (services[0], real_wp(s["wp_site"]), services[2])
    graph, wp, _http = services
    tokens = tokens or load_tokens(gcfg)
    ts = int(datetime.fromisoformat(s["slot_iso"]).timestamp())
    L = s["langs"][lang]
    try:
        txt = json.loads(gcfg.path(f"content/{key[:10]}/{g}/{key[11:13]}{key[14:16]}-{lang}.json")
                         .read_text(encoding="utf-8"))["fb_text"]
    except (OSError, KeyError, ValueError):
        txt = ""
    out, errors = [], []
    for pid, e in (L.get("pages") or {}).items():
        name = gcfg.page(pid).name if gcfg.page(pid) else pid
        if e.get("status") == "published" or e.get("status") == "done":
            out.append(f"- {name}: пост уже вышел — не трогаю")
            continue
        tok = tokens.get(pid)
        gone = False
        try:
            for sp in (graph.scheduled_posts(pid, tok) if tok else []):
                if int(sp.get("scheduled_publish_time") or 0) == ts and (
                        not txt or (sp.get("message") or "")[:60] == txt[:60]):
                    graph.delete(sp["id"], tok)
                    gone = True
        except FacebookError as err:
            errors.append(f"{name}: {err.short()}")
        e["status"] = "cancelled" if gone else e.get("status")
        out.append(f"- {name}: отложенный пост {'удалён' if gone else 'не найден'}")
    if L.get("wp_id"):
        try:
            wp.delete_post(L["wp_id"], force=True)
            out.append(f"- статья {L['wp_id']} удалена")
        except WordPressError as err:
            errors.append(f"статья {L['wp_id']}: {err}")
    s.setdefault("dropped_langs", {})[lang] = s["langs"].pop(lang)
    st.save()
    if errors:
        out.append("- ошибки: " + "; ".join(errors))
    return "\n".join(out)


def cmd_cancel_slot(cfg, key: str, services=None, tokens=None, state=None) -> int:
    """Отменяет подготовленный слот: удаляет отложенные посты Facebook и статьи WordPress, слот больше не трогается."""
    st = state or state_of(cfg)
    s = st.slots.get(key)
    if not s:
        print(f"Слот {key} не найден")
        return 1
    if services is None:
        services = real_services(cfg)
        if s.get("wp_site"):
            services = (services[0], real_wp(s["wp_site"]), services[2])
    graph, wp, _http = services
    tokens = tokens or load_tokens(cfg)
    ts = int(datetime.fromisoformat(s["slot_iso"]).timestamp())
    removed, kept, errors = 0, 0, []
    for lang, L in (s.get("langs") or {}).items():
        try:
            sub = f"{cfg.group}/" if cfg.group else ""
            txt = json.loads(cfg.path(f"content/{key[:10]}/{sub}{key[11:13]}{key[14:16]}-{lang}.json")
                             .read_text(encoding="utf-8"))["fb_text"]
        except (OSError, KeyError, ValueError):
            txt = ""
        for pid, e in (L.get("pages") or {}).items():
            if e.get("status") not in ("scheduled", "sending", "published", "failed"):
                continue
            tok = tokens.get(pid)
            if not tok:
                continue
            name = (cfg.page(pid).name if cfg.page(pid) else pid)
            gone = False
            try:
                for sp in graph.scheduled_posts(pid, tok):
                    same_time = int(sp.get("scheduled_publish_time") or 0) == ts
                    ours = not txt or (sp.get("message") or "")[:60] == txt[:60]
                    if same_time and ours:
                        graph.delete(sp["id"], tok)
                        gone = True
                if not gone and e.get("post_id"):
                    graph.delete(e["post_id"], tok)
                    gone = True
                if not gone and e.get("photo_id"):
                    graph.delete(e["photo_id"], tok)
                    gone = True
            except FacebookError as err:
                errors.append(f"{name}: {err.short()}")
            if gone:
                e["status"] = "cancelled"
                removed += 1
            elif e.get("status") in ("scheduled", "sending", "published"):
                kept += 1
            st.save()
        if L.get("wp_id") and not L.get("wp_cancelled"):
            try:
                wp.delete_post(L["wp_id"], force=True)
                L["wp_cancelled"] = True
            except WordPressError as err:
                errors.append(f"статья {lang} ({L['wp_id']}): {err}")
    s["status"] = "cancelled"
    st.save()
    left = []
    for lang, L in (s.get("langs") or {}).items():
        for pid in (L.get("pages") or {}):
            tok = tokens.get(pid)
            if not tok:
                continue
            try:
                if any(int(sp.get("scheduled_publish_time") or 0) == ts for sp in graph.scheduled_posts(pid, tok)):
                    left.append(cfg.page(pid).name if cfg.page(pid) else pid)
            except FacebookError:
                pass
    wp_gone = sum(1 for L in (s.get("langs") or {}).values() if L.get("wp_cancelled"))
    md = [f"## Отмена выпуска {key}", "",
          f"{'✅' if removed else '⚠️'} Удалено отложенных постов Facebook: **{removed}**",
          f"✅ Удалено статей на сайте: **{wp_gone}**"]
    if kept:
        md.append(f"❌ Не удалось удалить: {kept}")
    if left:
        md.append("❌ На этот час ещё что-то запланировано на страницах: " + "; ".join(left))
    else:
        md.append("✅ Проверка: на этот час на страницах больше ничего не запланировано")
    md += [f"- {e}" for e in errors[:20]]
    grp = f"-{cfg.group}" if cfg.group else ""
    summary("\n".join(md) + "\n", f"previews/cancel-{key[:10]}-{key[11:13]}{key[14:16]}{grp}.md", cfg)
    return 1 if (kept or left) else 0


def cmd_check_queue(cfg) -> int:
    """Проверяет очереди queue/<группа>.txt: читается ли каждый пост (текст + фото). Ничего не публикует."""
    graph, _wp, _http = real_services(cfg)
    tokens = load_tokens(cfg)
    p2p = cfg.profile_to_page()
    out = ["## Очереди ссылок по группам", ""]
    bad = 0
    for g, st in _group_states(cfg).items():
        gcfg = config_mod.group_view(cfg, g)
        lines, waiting = [], 0
        for link in manual_links(gcfg):
            try:
                info = parse_link(normalize_link(link, st), p2p)
                key = source_key(info["page_id"], info["part"])
                rec = st.sources.get(key) or {}
                if st.source_used(key) or rec.get("status") == "reserved":
                    lines.append(f"- ⏭ уже взята ({rec.get('status')}, {rec.get('slot', '')}): {link[:90]}")
                    continue
                post = fetch_post(graph, info, tokens, cfg.fallback_source_page_id)
                page = cfg.page(post.get("owner_page_id") or info["page_id"])
                waiting += 1
                first = " ".join(post["text"].split())[:100]
                lines.append(f"- ✅ {page.name if page else 'чужая страница'} · {len(post['text'])} знаков, "
                             f"фото есть · «{first}…»")
            except SourceError as e:
                bad += 1
                lines.append(f"- ❌ {link[:90]}: {e}")
        out.append(f"### {cfg.group_label(g)} ({len(gcfg.pages)} стр.) — ждут публикации: {waiting}")
        out += lines or ["- (очередь пуста)"]
        out.append("")
    summary("\n".join(out) + "\n", "previews/queue.md", cfg)
    return 1 if bad else 0


def cmd_sheet_status(cfg) -> int:
    """Проверка Google-таблицы: доступна ли, сколько старых строк пропущено, какие новые ссылки ждут."""
    st = state_of(cfg)
    out = ["## Google-таблица FB_QUEUE", ""]
    try:
        links = google_sheet_links(cfg)
    except Exception as e:  # noqa: BLE001
        out.append(f"❌ {e}")
        summary("\n".join(out) + "\n", "previews/sheet-status.md", cfg)
        return 1
    out.append(f"✅ Таблица читается, ссылок в колонке A: {len(links)}")
    if not st.data.get("sheet_baseline_at"):
        n = sheet_baseline(st, links, datetime.now(timezone.utc))
        out.append(f"✅ Подключено: {n} старых ссылок помечены как уже бывшие — они не публикуются. "
                   "Теперь добавляйте новые ссылки в конец колонки A.")
    else:
        out.append(f"Подключена с {st.data['sheet_baseline_at']}")
    waiting = [ln for ln in links if not st.source_used(link_key(ln))]
    out.append(f"Новых ссылок в очереди: {len(waiting)}")
    out += [f"- {ln[:100]}" for ln in waiting[:20]]
    summary("\n".join(out) + "\n", "previews/sheet-status.md", cfg)
    return 0


def cmd_render_workflow(cfg, args) -> int:
    path = write_workflow(cfg)
    print(f"Обновлено расписание в {path}:\n{render_cron_block(cfg)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="autopost")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("setup-facebook")
    p = sub.add_parser("private-test")
    p.add_argument("--source", default="")
    p.add_argument("--page", default="")
    p.add_argument("--keep", action="store_true")
    p.add_argument("--backend", default=None)
    p = sub.add_parser("public-test")
    p.add_argument("--test-id", default="")
    p.add_argument("--page", default="")
    p.add_argument("--confirm", default="")
    sub.add_parser("tick")
    sub.add_parser("status")
    p = sub.add_parser("simulate")
    p.add_argument("--days", type=int, default=2)
    sub.add_parser("render-workflow")
    sub.add_parser("ops")
    p = sub.add_parser("set-schedule")
    p.add_argument("mode", choices=["on", "off"])
    args = ap.parse_args(argv)
    cfg = config_mod.load()
    fn = {"check": cmd_check, "setup-facebook": cmd_setup_facebook, "private-test": cmd_private_test,
          "public-test": cmd_public_test, "tick": cmd_tick, "status": cmd_status, "simulate": cmd_simulate,
          "render-workflow": cmd_render_workflow, "set-schedule": cmd_set_schedule, "ops": cmd_ops}[args.cmd]
    try:
        return fn(cfg, args)
    except (TokenError, AuthProblem) as e:
        summary(f"❌ {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
