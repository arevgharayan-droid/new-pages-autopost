"""Токены страниц Facebook.

Один раз: токен системного пользователя бизнес-портфолио (Business Suite → Системные пользователи →
«Сгенерировать маркер», срок «Никогда») или токен пользователя из Graph API Explorer
→ токены 34 страниц (бессрочные).
Токены страниц хранятся ЗАШИФРОВАННЫМИ в state/page_tokens.enc; ключ шифрования выводится
из секрета FB_APP_SECRET, который есть только в GitHub Secrets.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

from .fb import FacebookError

NEEDED_TASKS = {"CREATE_CONTENT", "MODERATE"}
TOKENS_FILE = "state/page_tokens.enc"


class TokenError(Exception):
    pass


def _fernet(app_secret: str) -> Fernet:
    if not app_secret:
        raise TokenError("не задан секрет FB_APP_SECRET")
    key = hashlib.sha256(("interes-world-autopost/page-tokens/v1:" + app_secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def save_tokens(cfg, app_secret: str, tokens: dict, meta: dict) -> None:
    blob = json.dumps({"tokens": tokens, "meta": meta}).encode()
    p = cfg.path(TOKENS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_fernet(app_secret).encrypt(blob))


def extra_token_files(cfg) -> list:
    """Дополнительные наборы токенов (например, старого системного пользователя autopost):
    settings.yaml → facebook.extra_token_files. Из них берутся только страницы, которых нет в основном наборе."""
    return [str(x) for x in ((cfg.settings.get("facebook") or {}).get("extra_token_files") or []) if x]


def _read_tokens_file(cfg, rel: str, app_secret: str, with_meta: bool = False):
    data = json.loads(_fernet(app_secret).decrypt(cfg.path(rel).read_bytes()))
    return (data["tokens"], data.get("meta") or {}) if with_meta else data["tokens"]


def token_origins(cfg, app_secret: str | None = None) -> dict:
    """page_id → какой набор даёт токен ('main' или имя доп. файла)."""
    app_secret = app_secret if app_secret is not None else os.environ.get("FB_APP_SECRET", "")
    main, meta = _read_tokens_file(cfg, TOKENS_FILE, app_secret, with_meta=True)
    owners = meta.get("owners") or {}
    # основной набор: кто именно из пользователей (autopost2 / autopost3) дал токен; по-старому — "main"
    out = {pid: owners.get(pid) or "main" for pid in main}
    for rel in extra_token_files(cfg):
        try:
            for pid in _read_tokens_file(cfg, rel, app_secret):
                out.setdefault(pid, rel)
        except (OSError, InvalidToken, ValueError, KeyError):
            continue
    return out


def load_tokens(cfg, app_secret: str | None = None) -> dict:
    app_secret = app_secret if app_secret is not None else os.environ.get("FB_APP_SECRET", "")
    p = cfg.path(TOKENS_FILE)
    if not p.exists():
        raise TokenError("токены страниц ещё не получены — запустите workflow «2 · Подключить Facebook»")
    try:
        tokens = _read_tokens_file(cfg, TOKENS_FILE, app_secret)
    except InvalidToken:
        raise TokenError("не удаётся расшифровать токены: FB_APP_SECRET изменился — повторите «2 · Подключить Facebook»")
    # страницы, которых нет у основного пользователя, — из дополнительных наборов (первый найденный)
    for rel in extra_token_files(cfg):
        try:
            for pid, tok in _read_tokens_file(cfg, rel, app_secret).items():
                if tok and not tokens.get(pid):
                    tokens[pid] = tok
        except (OSError, InvalidToken, ValueError, KeyError):
            continue   # повреждённый или чужой доп. набор не мешает основному
    return tokens


def setup_facebook(cfg, graph, app_id: str, app_secret: str, user_token) -> dict:
    """Получает и сохраняет токены страниц. Возвращает отчёт для пользователя.

    user_token — один токен или список: несколько системных пользователей (autopost2, autopost3…) делят страницы
    между собой; страница берётся у первого, кому она назначена."""
    # позиция в списке = кто это (FB_USER_TOKEN → autopost2, _2 → autopost3, _3 → autopost4), пустые пропускаем
    user_tokens = [(i, t) for i, t in enumerate([user_token] if isinstance(user_token, str) else list(user_token)) if t]
    if not (app_id and app_secret and user_tokens):
        raise TokenError("нужны секреты FB_APP_ID, FB_APP_SECRET и FB_USER_TOKEN")
    by_id, per_user, owner_of = {}, [], {}
    labels = list(((cfg.settings.get("facebook") or {}).get("user_labels")) or ["autopost2", "autopost3", "autopost4"])
    for idx, ut in user_tokens:
        try:
            long_user = graph.exchange_long_lived(app_id, app_secret, ut)
        except Exception:  # noqa: BLE001 — токен системного пользователя обменивать не нужно, он бессрочный
            long_user = ut
        try:
            pages = graph.my_pages(long_user)
        except FacebookError as e:
            if len(user_tokens) == 1:
                raise
            per_user.append(f"ошибка ({e.short()})")   # один неверный токен не мешает остальным
            continue
        per_user.append(len(pages))
        for p in pages:
            if p["id"] not in by_id:
                by_id[p["id"]] = p
                owner_of[p["id"]] = labels[idx] if idx < len(labels) else f"user{idx + 1}"
    tokens, missing, no_rights, ok = {}, [], [], []
    for page in cfg.all_pages:
        got = by_id.get(page.page_id)
        if page.disabled:
            # публикации на неё нет, но токен храним: через неё читаются ссылки на чужие посты (как в Make)
            if got:
                tokens[page.page_id] = got["access_token"]
            continue
        if not got:
            missing.append(page)
            continue
        tasks = set(got.get("tasks") or [])
        if tasks and not NEEDED_TASKS.issubset(tasks):
            no_rights.append((page, sorted(NEEDED_TASKS - tasks)))
        tokens[page.page_id] = got["access_token"]
        ok.append(page)
    expires = None
    if tokens:
        any_tok = next(iter(tokens.values()))
        try:
            info = graph.debug_token(any_tok, app_id, app_secret)
            expires = info.get("expires_at")
            scopes = info.get("scopes") or []
        except Exception:  # noqa: BLE001 — отчёт не должен падать из-за диагностики
            scopes = []
    else:
        scopes = []
    meta = {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pages": len(tokens), "expires_at": expires,
            "owners": {pid: owner_of.get(pid) for pid in tokens if owner_of.get(pid)}}
    if tokens:
        save_tokens(cfg, app_secret, tokens, meta)
    extra = sorted(set(by_id) - {p.page_id for p in cfg.all_pages})
    return {"ok": ok, "missing": missing, "no_rights": no_rights, "expires_at": expires, "per_user": per_user,
            "scopes": scopes, "extra_pages": [(i, by_id[i].get("name")) for i in extra]}
