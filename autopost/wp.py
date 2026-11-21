"""Клиент WordPress REST API (пароль приложения): медиа и записи."""
from __future__ import annotations

import re
import time

import requests


class WordPressError(Exception):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def transient(self) -> bool:
        return self.status is None or self.status >= 500 or self.status == 429


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "image"
    return name[:80]


class WordPress:
    def __init__(self, base_url, user, app_password, session=None, retries=3, backoff=4.0, timeout=90,
                 api_key=None):
        self.api = base_url.rstrip("/") + "/wp-json/wp/v2"
        self.http = session or requests.Session()
        # Ключ плагина Make Connector (заголовок IWC-API-KEY) работает и там, где хостинг
        # вырезает заголовок Authorization; иначе — пароль приложения WordPress.
        self.api_key = (api_key or "").strip() or None
        self.auth = None if self.api_key else (user, (app_password or "").replace(" ", ""))
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout

    def _req(self, method, path, idempotent=True, **kw):
        url = f"{self.api}/{path.lstrip('/')}"
        last = None
        for attempt in range(1, self.retries + 1):
            try:
                if self.api_key:
                    kw = dict(kw)
                    kw["headers"] = {**(kw.get("headers") or {}), "IWC-API-KEY": self.api_key}
                r = self.http.request(method, url, auth=self.auth, timeout=self.timeout, **kw)
            except requests.RequestException as e:
                last = WordPressError(f"сеть: {e}")
            else:
                if r.status_code < 400:
                    return r.json() if r.content else {}
                try:
                    body = r.json()
                except ValueError:
                    body = {"message": r.text[:300]}
                last = WordPressError(f"{method} {path}: " + str(body.get("message", f"HTTP {r.status_code}")),
                                      r.status_code, body.get("code"))
                if not last.transient:
                    raise last
            if not idempotent:
                raise last
            if attempt < self.retries:
                time.sleep(self.backoff * attempt)
        raise last

    def me(self) -> dict:
        return self._req("GET", "users/me", params={"context": "edit"})

    def upload_media(self, data: bytes, filename: str, mime: str, title: str = "", alt: str = "") -> dict:
        if self.api_key:
            # через Make Connector сырое тело не проходит — отправляем как multipart/form-data;
            # плагин отвечает «сырым» объектом WP_Post (ID вместо id) — приводим к обычному виду
            raw = self._req("POST", "media", idempotent=False,
                            files={"file": (_safe_filename(filename), data, mime)})
            mid = raw.get("id") or raw.get("ID")
            if not mid:
                raise WordPressError(f"POST media: неожиданный ответ {str(raw)[:200]}")
            media = self._req("GET", f"media/{mid}", params={"context": "edit"})
        else:
            headers = {"Content-Disposition": f'attachment; filename="{_safe_filename(filename)}"',
                       "Content-Type": mime}
            media = self._req("POST", "media", idempotent=False, data=data, headers=headers)
        if title or alt:
            try:
                self._req("POST", f"media/{media['id']}", json={"title": title[:180], "alt_text": alt[:180]})
            except WordPressError:
                pass
        return media

    def create_post(self, title, content, status="draft", date_gmt=None, featured_media=None,
                    categories=None, slug=None) -> dict:
        payload = {"title": title, "content": content, "status": status}
        if slug:
            payload["slug"] = slug
        if date_gmt:
            payload["date_gmt"] = date_gmt
        if featured_media:
            payload["featured_media"] = int(featured_media)
        if categories:
            payload["categories"] = [int(c) for c in categories]
        return self._req("POST", "posts", idempotent=False, json=payload)

    def get_post(self, post_id) -> dict:
        return self._req("GET", f"posts/{post_id}", params={"context": "edit"})

    def update_post(self, post_id, **fields) -> dict:
        return self._req("POST", f"posts/{post_id}", json=fields)

    def delete_post(self, post_id, force=True) -> dict:
        return self._req("DELETE", f"posts/{post_id}", params={"force": "true" if force else "false"})

    def update_media(self, media_id, **fields) -> dict:
        return self._req("POST", f"media/{media_id}", json=fields)

    def delete_media(self, media_id) -> dict:
        return self._req("DELETE", f"media/{media_id}", params={"force": "true"})

    def find_media_by_slug(self, slug) -> list:
        for variant in (slug, f"{slug}-jpg", f"{slug}-jpeg", f"{slug}-png", f"{slug}-webp"):
            found = self._req("GET", "media", params={"slug": variant, "per_page": 5})
            if found:
                return found
        return []

    def find_by_slug(self, slug) -> list:
        """Поиск записи по постоянному ярлыку — защита от дублей статей."""
        return self._req("GET", "posts", params={"slug": slug, "status": "publish,future,draft,pending,private",
                                                  "context": "edit", "per_page": 5})
