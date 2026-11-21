"""Клиент Facebook Graph API: страницы, фото-посты (в т.ч. отложенные), комментарии."""
from __future__ import annotations

import json
import re
import time

import requests

_SECRET_RE = re.compile(r"((?:access_token|client_secret|input_token|fb_exchange_token)=)[^&\s\"'<>]+")


def redact(text) -> str:
    """Убирает токены и секреты из любого текста (сообщения об ошибках, URL)."""
    return _SECRET_RE.sub(r"\1***", str(text))

# Коды ошибок Graph API, при которых имеет смысл повторить запрос позже.
TRANSIENT_CODES = {1, 2, 4, 17, 32, 341, 613}
AUTH_CODES = {102, 190}
PERMISSION_CODES = {10, 200, 210, 230}


class FacebookError(Exception):
    def __init__(self, message, code=None, subcode=None, status=None, fbtrace=None):
        super().__init__(redact(message))
        self.code = code
        self.subcode = subcode
        self.status = status
        self.fbtrace = fbtrace

    @property
    def transient(self) -> bool:
        return self.code in TRANSIENT_CODES or (self.status or 0) >= 500 or self.code is None

    @property
    def rate_limited(self) -> bool:
        """368 — Facebook временно ограничил частоту публикаций («Мы ограничиваем частоту…»)."""
        return self.code == 368

    @property
    def auth(self) -> bool:
        return self.code in AUTH_CODES

    def short(self) -> str:
        return f"FB {self.code}/{self.subcode}: {self}"


class Graph:
    def __init__(self, version="v26.0", session=None, retries=3, backoff=4.0, timeout=90):
        self.base = f"https://graph.facebook.com/{version}"
        self.http = session or requests.Session()
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout

    # --- низкий уровень -------------------------------------------------------
    def _call(self, method, path, token, params=None, data=None, files=None):
        url = path if path.startswith("http") else f"{self.base}/{path.lstrip('/')}"
        params = dict(params or {})
        if token and method == "POST":
            data = dict(data or {})
            data["access_token"] = token  # в теле запроса, а не в URL
        elif token:
            params["access_token"] = token
        last = None
        for attempt in range(1, self.retries + 1):
            try:
                if method == "GET":
                    r = self.http.get(url, params=params, timeout=self.timeout)
                elif method == "DELETE":
                    r = self.http.delete(url, params=params, timeout=self.timeout)
                else:
                    r = self.http.post(url, params=params, data=data, files=files, timeout=self.timeout)
            except requests.RequestException as e:
                last = FacebookError(f"сеть: {type(e).__name__}: {redact(e)}")
            else:
                try:
                    body = r.json()
                except ValueError:
                    body = {"error": {"message": r.text[:300]}}
                if r.status_code < 400 and not (isinstance(body, dict) and "error" in body):
                    return body
                err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
                last = FacebookError(err.get("message", f"HTTP {r.status_code}"), err.get("code"),
                                     err.get("error_subcode"), r.status_code, err.get("fbtrace_id"))
                if not last.transient:
                    raise last
            # POST с файлом не повторяем вслепую: результат мог примениться.
            if method == "POST" and not getattr(self, "_retry_posts", False):
                raise last
            if attempt < self.retries:
                time.sleep(self.backoff * attempt)
        raise last

    def get(self, path, token, **params):
        return self._call("GET", path, token, params=params)

    def post(self, path, token, data=None, files=None, **params):
        return self._call("POST", path, token, params=params, data=data, files=files)

    def delete(self, path, token):
        return self._call("DELETE", path, token)

    def paged(self, path, token, limit_pages=20, **params):
        body = self.get(path, token, **params)
        out = list(body.get("data", []))
        n = 1
        while body.get("paging", {}).get("next") and n < limit_pages:
            body = self._call("GET", body["paging"]["next"], None)
            out.extend(body.get("data", []))
            n += 1
        return out

    # --- токены -----------------------------------------------------------------
    def exchange_long_lived(self, app_id, app_secret, user_token) -> str:
        body = self.get("oauth/access_token", None, grant_type="fb_exchange_token",
                        client_id=app_id, client_secret=app_secret, fb_exchange_token=user_token)
        return body["access_token"]

    def debug_token(self, token, app_id, app_secret) -> dict:
        return self.get("debug_token", f"{app_id}|{app_secret}", input_token=token).get("data", {})

    def my_pages(self, user_token) -> list:
        return self.paged("me/accounts", user_token, fields="id,name,access_token,tasks", limit=100)

    # --- чтение постов ----------------------------------------------------------
    POST_FIELDS = "id,message,full_picture,created_time,permalink_url,attachments{media_type,type,description}"

    def get_post(self, post_id, token) -> dict:
        return self.get(post_id, token, fields=self.POST_FIELDS)

    def get_photo(self, photo_id, token) -> dict:
        return self.get(photo_id, token, fields="id,name,images,page_story_id,link,created_time,from")

    def page_posts(self, page_id, token, since_ts: int, limit=50) -> list:
        return self.get(f"{page_id}/posts", token, fields=self.POST_FIELDS,
                        since=since_ts, limit=limit).get("data", [])

    def is_published(self, post_id, token) -> bool:
        body = self.get(post_id, token, fields="id,is_published")
        return body.get("is_published") is True

    def reschedule_post(self, post_id, token, ts: int) -> dict:
        """Перенести отложенный пост на другое время (без удаления и повторной публикации)."""
        return self.post(post_id, token, data={"scheduled_publish_time": str(int(ts))})

    def scheduled_posts(self, page_id, token) -> list:
        return self.paged(f"{page_id}/scheduled_posts", token,
                          fields="id,message,scheduled_publish_time", limit=100)

    # --- публикация ---------------------------------------------------------------
    def create_photo_post(self, page_id, token, image: bytes, filename: str, caption: str,
                          scheduled_ts: int | None = None, mime: str = "image/jpeg") -> dict:
        files = {"source": (filename, image, mime)}
        if not scheduled_ts:
            return self.post(f"{page_id}/photos", token, data={"caption": caption, "published": "true"}, files=files)
        # Отложенный пост с фото — как рекомендует Meta: фото загружается неопубликованным (temporary),
        # затем создаётся отложенная запись в ЛЕНТЕ с этим фото. (Отложенное фото через /photos
        # выходит только во вкладку «Фото» без записи в ленте — «no timeline unit».)
        photo = self.post(f"{page_id}/photos", token, files=files,
                          data={"published": "false", "temporary": "true"})
        post = self.post(f"{page_id}/feed", token, data={
            "message": caption,
            "attached_media[0]": json.dumps({"media_fbid": photo["id"]}),
            "published": "false",
            "scheduled_publish_time": str(int(scheduled_ts)),
        })
        return {"id": photo["id"], "post_id": post.get("id")}

    def page_story_id(self, photo_id, token) -> str | None:
        return self.get(photo_id, token, fields="page_story_id").get("page_story_id")

    def comment(self, obj_id, token, message) -> dict:
        return self.post(f"{obj_id}/comments", token, data={"message": message})

    def comments(self, obj_id, token) -> list:
        return self.get(f"{obj_id}/comments", token, fields="id,message,from", limit=50,
                        filter="toplevel").get("data", [])
