"""Откуда берутся новые истории: ручная очередь, старая Google-таблица, авто-режим по вашим страницам."""
from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import datetime, timedelta, timezone

import requests

from .fb import FacebookError
from .state import text_fingerprint


class SourceError(Exception):
    pass


def parse_link(link: str, profile_to_page: dict) -> dict:
    """Разбор ссылки так же, как в модуле 2 Make (+ запасные варианты).

    Возвращает {"raw_page": ..., "page_id": ..., "part": ..., "candidates": [ids для запроса]}.
    """
    link = (link or "").strip()
    if not link:
        raise SourceError("пустая ссылка")
    if "facebook.com" in link:
        if "/share/" in link or "pfbid" in link:
            raise SourceError("короткая ссылка /share/ или pfbid не содержит числовых ID — "
                              "откройте пост и скопируйте полную ссылку (как и в Make)")
        if "set=pb." in link:
            raw_page = link.split("set=pb.", 1)[1].split(".", 1)[0]
        elif "story_fbid=" in link:
            m = re.search(r"[?&]id=(\d+)", link)
            raw_page = m.group(1) if m else ""
        else:
            raw_page = link.split("facebook.com/", 1)[1].split("/", 1)[0].split("?", 1)[0]
        m = re.search(r"fbid=(\d+)", link)
        if m:
            part = m.group(1)
        elif "/posts/" in link:
            part = link.split("/posts/", 1)[1].split("?", 1)[0].strip("/")
        else:
            m = re.search(r"/(?:photos|photo)/(?:[^/]+/)?(\d+)", link)
            part = m.group(1) if m else ""
    else:
        if "_" not in link:
            raise SourceError("ожидается ссылка facebook.com или ID вида PAGEID_POSTID")
        raw_page, part = link.split("_", 1)
    if not part or not re.fullmatch(r"\d+", part):
        raise SourceError(f"не удалось найти числовой ID поста в ссылке: {link[:120]}")
    page_id = profile_to_page.get(raw_page, raw_page)
    cands = []
    for pid in (f"{page_id}_{part}", f"{raw_page}_{part}", part):
        if pid not in cands:
            cands.append(pid)
    return {"raw_page": raw_page, "page_id": page_id, "part": part, "candidates": cands}


def resolve_share_link(link: str) -> dict:
    """facebook.com/share/… → {"url": полная ссылка, "story_fbid", "id", "title"} (без входа в Facebook).

    Facebook отдаёт короткие ссылки своему сборщику предпросмотра (как при вставке ссылки в пост),
    поэтому пробуем несколько вариантов «браузера»."""
    import html as _html
    import urllib.parse
    agents = ["facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
              "Twitterbot/1.0",
              "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"]
    found = {}
    for ua in agents:
        url = link
        try:
            for _ in range(6):
                r = requests.get(url, headers={"User-Agent": ua, "Accept-Language": "en-US,en;q=0.8"},
                                 allow_redirects=False, timeout=25)
                loc = r.headers.get("Location") or ""
                if loc:
                    if loc.startswith("/"):
                        loc = "https://www.facebook.com" + loc
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
                    if "login" in loc and q.get("next"):
                        loc = q["next"][0]
                    url = loc
                    if "/share/" not in loc and "login" not in loc:
                        found.setdefault("url", loc)
                    continue
                text = r.text
                for pat in (r'<meta property="og:url" content="([^"]+)"', r'<link rel="canonical" href="([^"]+)"'):
                    m = re.search(pat, text)
                    if m and "/share/" not in m.group(1):
                        found.setdefault("url", _html.unescape(m.group(1)))
                m = re.search(r'<meta property="og:title" content="([^"]*)"', text)
                if m:
                    found.setdefault("title", _html.unescape(m.group(1))[:120])
                for key, pat in (("story_fbid", r'"story_fbid":"?(\d+)'), ("post_id", r'"post_id":"(\d+)"'),
                                 ("page_id", r'"pageID":"(\d+)"')):
                    m = re.search(pat, text)
                    if m:
                        found.setdefault(key, m.group(1))
                break
        except requests.RequestException:
            continue
        if found.get("url"):
            break
    u = found.get("url", "")
    m = re.search(r"story_fbid=(\d+)", u)
    if m:
        found["story_fbid"] = m.group(1)
    m = re.search(r"[?&]id=(\d+)", u)
    if m:
        found["id"] = m.group(1)
    return found


def _pfbid_parts(link: str):
    """story.php?story_fbid=pfbid…&id=PAGE  или  facebook.com/PAGE/posts/pfbid… → (PAGE, pfbid)."""
    m = re.search(r"(pfbid[0-9A-Za-z]+)", link)
    if not m:
        return "", ""
    pf = m.group(1)
    page = ""
    q = re.search(r"[?&]id=(\d+)", link)
    if q:
        page = q.group(1)
    else:
        q = re.search(r"facebook\.com/([^/?]+)/posts/", link)
        if q:
            page = q.group(1)
    return page, pf


def resolve_pfbid_link(link: str, graph=None, tokens=None, fallback_page_id: str = "") -> str:
    """Ссылка с pfbid (новый формат Facebook, без числового ID) → "PAGEID_POSTID".
    1) как короткую ссылку — через предпросмотр Facebook; 2) по списку постов страницы через Graph API."""
    page, pf = _pfbid_parts(link)
    if not pf:
        raise SourceError("в ссылке нет ID поста")
    # Facebook перенаправляет …/PAGE/posts/pfbid… на адрес с числовым ID поста — пробуем и этот вид ссылки
    variants = [link]
    if page:
        variants.append(f"https://www.facebook.com/{page}/posts/{pf}")
    for v in variants:
        info = resolve_share_link(v)
        if info.get("story_fbid") and (info.get("id") or page):
            return f"{info.get('id') or page}_{info['story_fbid']}"
        if info.get("post_id") and (info.get("page_id") or page):
            return f"{info.get('page_id') or page}_{info['post_id']}"
        u = info.get("url") or ""
        m = re.search(r"story_fbid=(\d+)", u)
        if m and (re.search(r"[?&]id=(\d+)", u) or page):
            pid = re.search(r"[?&]id=(\d+)", u)
            return f"{pid.group(1) if pid else page}_{m.group(1)}"
        m = re.search(r"facebook\.com/([^/?]+)/posts/(?:[^/?]+/)?(\d{8,})", u)
        if m:
            return f"{m.group(1) if m.group(1).isdigit() else page}_{m.group(2)}"
    if graph is not None and tokens and page:
        token = tokens.get(page) or tokens.get(fallback_page_id) or next(iter(tokens.values()), None)
        try:
            posts = graph.get(f"{page}/posts", token, fields="id,permalink_url", limit=100).get("data", [])
        except FacebookError as e:
            raise SourceError(f"ссылку pfbid не удалось раскрыть: {e.short()}")
        for p in posts:
            if pf in (p.get("permalink_url") or ""):
                return p["id"]
        raise SourceError("ссылку pfbid не удалось раскрыть: пост не найден среди последних 100 постов страницы")
    raise SourceError("ссылку pfbid не удалось раскрыть — пришлите ссылку «Поделиться» (facebook.com/share/…)")


def normalize_link(link: str, state, graph=None, tokens=None, fallback_page_id: str = "") -> str:
    """Короткую ссылку facebook.com/share/… (и ссылку с pfbid) раскрывает в полную (запоминается в состоянии)."""
    is_pf = "pfbid" in link and "/share/" not in link
    if "/share/" not in link and not is_pf:
        return link
    key = link_key(link)
    rec = state.sources.get(key) or {}
    if rec.get("resolved"):
        return rec["resolved"]
    if is_pf:
        url = resolve_pfbid_link(link, graph, tokens, fallback_page_id)
        state.sources.setdefault(key, {}).update(resolved=url, link=link)
        state.save()
        return url
    info = resolve_share_link(link)
    if info.get("story_fbid") and info.get("id"):
        url = f"https://www.facebook.com/story.php?story_fbid={info['story_fbid']}&id={info['id']}"
    elif info.get("url") and "/share/" not in info["url"]:
        url = info["url"]
    else:
        raise SourceError("короткую ссылку /share/ не удалось раскрыть — пришлите полную ссылку на пост")
    if "pfbid" in url and not re.search(r"story_fbid=\d+", url):
        url = resolve_pfbid_link(url, graph, tokens, fallback_page_id)
    state.sources.setdefault(key, {}).update(resolved=url, link=link)
    state.save()
    return url


def link_key(link: str) -> str:
    return "link:" + hashlib.sha1(link.strip().encode("utf-8")).hexdigest()[:20]


def sheet_baseline(state, links: list, now: datetime) -> int:
    """Отмечает все ссылки, уже стоящие в таблице, как старые. Возвращает их число."""
    n = 0
    for link in links:
        key = link_key(link)
        if not state.source_used(key):
            state.data.setdefault("sources", {})[key] = {"status": "skipped", "link": link,
                                                         "error": "была в таблице до подключения"}
            n += 1
    state.data["sheet_baseline_at"] = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    state.save()
    return n


def parse_fb_time(value) -> float | None:
    v = str(value or "").strip()
    if not v:
        return None
    if v.isdigit():
        return float(v)
    try:
        return datetime.strptime(v, "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except ValueError:
        try:
            return datetime.fromisoformat(v).timestamp()
        except ValueError:
            return None


def source_key(page_id: str, part: str) -> str:
    return f"post:{page_id}_{part}"


def _has_photo(post: dict) -> bool:
    if not post.get("full_picture"):
        return False
    att = ((post.get("attachments") or {}).get("data") or [{}])[0]
    mt = (att.get("media_type") or att.get("type") or "photo").lower()
    return mt in ("photo", "album", "image") or "photo" in mt


def fetch_post(graph, link_info: dict, tokens: dict, fallback_page_id: str) -> dict:
    """Читает текст и фото поста. Нужен токен страницы-владельца (ваши страницы)."""
    own = tokens.get(link_info["page_id"])
    # чужие публичные посты читаем токеном страницы-«читателя»; если её нет у системного пользователя —
    # любой из подключённых страниц (у autopost2/autopost3 может не быть Émotions Virales)
    token = own or tokens.get(fallback_page_id) or next(iter(tokens.values()), None)
    if not token:
        raise SourceError(f"нет доступа к странице {link_info['page_id']} (она не среди подключённых)")
    # ссылка на фото без ID страницы (photo/?fbid=…&set=a.…): пробуем запасную страницу, затем все свои
    photo_tokens = [token] if own else [token] + [t for pid, t in tokens.items() if t != token]
    errors = []
    for pid in link_info["candidates"]:
        if pid == link_info["part"]:
            for tok in photo_tokens:
                try:
                    ph = graph.get_photo(pid, tok)
                except FacebookError as e:
                    if len(errors) < 3:
                        errors.append(f"{pid}: {e.short()}")
                    continue
                imgs = sorted(ph.get("images") or [], key=lambda i: i.get("width", 0), reverse=True)
                if ph.get("name") and imgs:
                    return {"text": ph["name"], "image_url": imgs[0]["source"],
                            "post_id": ph.get("page_story_id") or pid, "created_time": ph.get("created_time"),
                            "permalink": ph.get("link"), "owner_page_id": (ph.get("from") or {}).get("id")}
                errors.append(f"{pid}: у фото нет подписи (текста истории)")
                break
            continue
        try:
            post = graph.get_post(pid, token)
            # 19.11: у части постов текста в message нет — он лежит в описании фото (attachments[0].description)
            att = ((post.get("attachments") or {}).get("data") or [{}])[0]
            text = post.get("message") or (att.get("description") if att.get("media_type") == "photo" else "")
            if text and post.get("full_picture"):
                return {"text": text, "image_url": post["full_picture"], "post_id": post["id"],
                        "created_time": post.get("created_time"), "permalink": post.get("permalink_url")}
            errors.append(f"{pid}: нет текста или фото")
        except FacebookError as e:
            errors.append(f"{pid}: {e.short()}")
    raise SourceError("не удалось прочитать пост: " + " | ".join(errors[-3:]))


def manual_links(cfg) -> list:
    p = cfg.path(cfg.sources.get("manual_file", "queue/links.txt"))
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def google_sheet_links(cfg, session=None) -> list:
    gs = cfg.sources.get("google_sheet") or {}
    if not gs.get("enabled"):
        return []
    url = (f"https://docs.google.com/spreadsheets/d/{gs['spreadsheet_id']}/gviz/tq"
           f"?tqx=out:csv&sheet={gs.get('sheet', 'FB_QUEUE')}")
    r = (session or requests).get(url, timeout=30)
    if r.status_code != 200 or "<html" in r.text[:200].lower():
        raise SourceError("Google-таблица недоступна: откройте доступ «Все, у кого есть ссылка — читатель»")
    rows = list(csv.reader(io.StringIO(r.text)))
    return [row[0].strip() for row in rows[1:] if row and row[0].strip().startswith(("http", "1", "6"))]


def auto_candidates(cfg, graph, tokens: dict, state, now: datetime) -> list:
    a = cfg.sources.get("auto") or {}
    if not a.get("enabled"):
        return []
    pages = [p.page_id for p in cfg.pages] if a.get("pages", "all") == "all" else [str(x) for x in a["pages"]]
    since = int((now - timedelta(hours=int(a.get("lookback_hours", 72)))).timestamp())
    cutoff = a.get("min_created") or state.data.get("source_cutoff")
    if cutoff:
        since = max(since, int(datetime.fromisoformat(str(cutoff)).timestamp()))
    min_chars = int(a.get("min_text_chars", 300))
    found = []
    for pid in pages:
        tok = tokens.get(pid)
        if not tok:
            continue
        try:
            posts = graph.page_posts(pid, tok, since)
        except FacebookError:
            continue
        for post in posts:
            msg = post.get("message") or ""
            created = parse_fb_time(post.get("created_time"))
            if created is not None and created < since:
                continue
            if len(msg) < min_chars:
                continue
            if a.get("require_photo", True) and not _has_photo(post):
                continue
            if state.is_our_output(post["id"], msg):
                continue
            part = post["id"].split("_")[-1]
            key = source_key(pid, part)
            if state.source_used(key) or state.fingerprint_used(text_fingerprint(msg)):
                continue
            if (state.sources.get(key) or {}).get("status") == "reserved":
                continue
            found.append({"key": key, "page_id": pid, "post": post, "created": post.get("created_time", "")})
    found.sort(key=lambda x: x["created"])  # самые старые новые — первыми
    return found


def pick_source(cfg, graph, tokens: dict, state, now: datetime, log) -> dict | None:
    """Выбирает следующую историю. Возвращает {"key", "page_id", "text", "image_url", "origin", "link"}."""
    p2p = cfg.profile_to_page()
    for origin in cfg.sources.get("order", ["manual", "google_sheet", "auto"]):
        if origin in ("manual", "google_sheet"):
            try:
                links = manual_links(cfg) if origin == "manual" else google_sheet_links(cfg)
            except SourceError as e:
                log(f"⚠️ {e}")
                continue
            if origin == "google_sheet" and not state.data.get("sheet_baseline_at"):
                # как watchRows в Make: строки, которые уже были в таблице при подключении, не публикуются
                n = sheet_baseline(state, links, now)
                log(f"Google-таблица подключена: {n} старых ссылок пропущено, дальше берутся только новые строки")
                continue
            for link in links:
                if origin == "google_sheet" and state.source_used(link_key(link)):
                    continue
                try:
                    info = parse_link(normalize_link(link, state, graph, tokens, cfg.fallback_source_page_id), p2p)
                except SourceError as e:
                    key = "bad:" + text_fingerprint(link)
                    if not state.source_used(key):
                        state.mark_source(key, status="skipped", link=link, error=str(e))
                        log(f"⚠️ ссылка пропущена: {e}")
                    continue
                key = source_key(info["page_id"], info["part"])
                if state.source_used(key) or (state.sources.get(key) or {}).get("status") == "reserved":
                    prev = state.sources.get(key) or {}
                    lrec = state.sources.setdefault(link_key(link), {})
                    if prev.get("link") and prev.get("link") != link and not lrec.get("dup_logged"):
                        log(f"⚠️ ссылка пропущена — тот же пост уже выходил в этой группе "
                            f"({prev.get('slot', '?')}, по ссылке {prev.get('link', '')[:60]}): {link[:80]}")
                        lrec["dup_logged"] = True      # сообщаем один раз, а не при каждом запуске
                    continue
                try:
                    post = fetch_post(graph, info, tokens, cfg.fallback_source_page_id)
                except SourceError as e:
                    state.mark_source(key, status="failed", link=link, error=str(e))
                    log(f"⚠️ {link[:80]}: {e}")
                    continue
                fp = text_fingerprint(post["text"])
                if state.fingerprint_used(fp):
                    state.mark_source(key, status="skipped", link=link, error="эта история уже публиковалась")
                    log(f"⚠️ ссылка пропущена — эта история уже выходила в этой группе: {link[:80]}")
                    continue
                if origin == "google_sheet":
                    state.mark_source(link_key(link), status="used", link=link, post_key=key)
                return {"key": key, "page_id": post.get("owner_page_id") or info["page_id"], "text": post["text"],
                        "image_url": post["image_url"], "origin": origin, "link": link, "fingerprint": fp}
        elif origin == "auto":
            for c in auto_candidates(cfg, graph, tokens, state, now):
                post = c["post"]
                # пост уже распространялся (есть комментарий со ссылкой на статью) — это не новая история
                try:
                    linked = any(any(d in (cm.get("message") or "") for d in ("interes-world.com/?p=", "peakproductions.co/?p=", "hetaqrqir.com/?p="))
                                 for cm in graph.comments(post["id"], tokens[c["page_id"]]))
                except FacebookError:
                    continue
                if linked:
                    state.mark_source(c["key"], status="skipped", error="уже распространялась (есть ссылка в комментарии)")
                    continue
                return {"key": c["key"], "page_id": c["page_id"], "text": post["message"],
                        "image_url": post["full_picture"], "origin": "auto",
                        "link": post.get("permalink_url") or post["id"],
                        "fingerprint": text_fingerprint(post["message"])}
    return None
