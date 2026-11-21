"""Поддельные Facebook / WordPress / загрузка картинок — для сухого прогона и тестов без сети."""
from __future__ import annotations

import itertools

from .fb import FacebookError
from .wp import WordPressError

_ids = itertools.count(900000000000001)


def nid() -> str:
    return str(next(_ids))


class FakeResponse:
    def __init__(self, content=b"\xff\xd8fakejpeg", status=200, ctype="image/jpeg"):
        self.content, self.status_code, self.headers = content, status, {"Content-Type": ctype}


class FakeHTTP:
    def __init__(self):
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        return FakeResponse()


class FakeGraph:
    """Имитирует страницы: отложенные посты выходят, когда «часы» доходят до времени публикации."""

    def __init__(self, cfg, clock):
        self.cfg, self.clock = cfg, clock
        self.posts = {}      # post_id -> dict(page, message, scheduled_ts, published, photo_id)
        self.comments_by = {}
        self.fail_next = []  # список (метод, page_id|None, FacebookError) для внедрения сбоев
        self.calls = []
        self.source_posts = {}  # page_id -> [post dict] (истории, опубликованные вручную)

    def _maybe_fail(self, method, page_id=None):
        for i, (m, p, err) in enumerate(self.fail_next):
            if m == method and (p is None or p == page_id):
                del self.fail_next[i]
                raise err

    def _publish_due(self):
        now = self.clock().timestamp()
        for p in self.posts.values():
            if not p["published"] and p["scheduled_ts"] and p["scheduled_ts"] <= now:
                p["published"] = True

    # токены
    def exchange_long_lived(self, app_id, app_secret, user_token):
        return "LONG-" + user_token

    def my_pages(self, user_token):
        return [{"id": p.page_id, "name": p.name, "access_token": f"PT-{p.page_id}",
                 "tasks": ["CREATE_CONTENT", "MODERATE", "MANAGE"]} for p in self.cfg.pages]

    def debug_token(self, token, app_id, app_secret):
        return {"expires_at": 0, "scopes": ["pages_manage_posts", "pages_manage_engagement"]}

    # чтение
    def page_posts(self, page_id, token, since_ts, limit=50):
        self.calls.append(("page_posts", page_id))
        self._maybe_fail("page_posts", page_id)
        self._publish_due()
        out = [p for p in self.source_posts.get(page_id, []) if p.get("_ts", 0) >= since_ts]
        out += [{"id": pid, "message": p["message"], "full_picture": "https://fb.example/img.jpg",
                 "created_time": str(int(p["scheduled_ts"] or p.get("created_ts", 0))),
                 "attachments": {"data": [{"media_type": "photo"}]}}
                for pid, p in self.posts.items() if p["page"] == page_id and p["published"]]
        return out

    def get_post(self, post_id, token):
        for posts in self.source_posts.values():
            for p in posts:
                if p["id"] == post_id:
                    return p
        raise FacebookError("Unsupported get request", code=100)

    def get_photo(self, photo_id, token):
        raise FacebookError("Unsupported get request", code=100)

    def scheduled_posts(self, page_id, token):
        self._publish_due()
        return [{"id": pid, "message": p["message"], "scheduled_publish_time": p["scheduled_ts"]}
                for pid, p in self.posts.items() if p["page"] == page_id and not p["published"]]

    def is_published(self, obj_id, token):
        self._publish_due()
        if obj_id not in self.posts:
            raise FacebookError("Tried accessing nonexisting field (is_published)", code=100)
        return self.posts[obj_id]["published"]

    def page_story_id(self, photo_id, token):
        for pid, p in self.posts.items():
            if p["photo_id"] == photo_id:
                return pid
        return None

    # запись
    def create_photo_post(self, page_id, token, image, filename, caption, scheduled_ts=None, mime="image/jpeg"):
        self.calls.append(("create_photo_post", page_id, scheduled_ts))
        assert token == f"PT-{page_id}", "токен не той страницы"
        assert image, "нет изображения"
        lost = None
        try:
            self._maybe_fail("create_photo_post", page_id)
        except FacebookError as err:
            if not getattr(err, "after_create", False):
                raise
            lost = err  # пост создастся, но ответ «потеряется» (обрыв связи)
        if scheduled_ts:
            lead = scheduled_ts - self.clock().timestamp()
            if lead < 600:
                raise FacebookError("scheduled time must be at least 10 minutes in the future", code=100)
        photo, post = nid(), f"{page_id}_{nid()}"
        self.posts[post] = {"page": page_id, "message": caption, "scheduled_ts": scheduled_ts,
                            "published": not scheduled_ts, "photo_id": photo,
                            "created_ts": self.clock().timestamp()}
        if lost:
            raise lost
        # как в настоящем Facebook: для отложенных фото post_id не возвращается
        return {"id": photo} if scheduled_ts else {"id": photo, "post_id": post}

    def comment(self, obj_id, token, message):
        self.calls.append(("comment", obj_id))
        page = self.posts[obj_id]["page"]
        lost = None
        try:
            self._maybe_fail("comment", page)
        except FacebookError as err:
            if not getattr(err, "after_create", False):
                raise
            lost = err
        assert self.posts[obj_id]["published"], "комментарий к неопубликованному посту"
        cid = f"{obj_id}_{nid()}"
        self.comments_by.setdefault(obj_id, []).append({"id": cid, "message": message})
        if lost:
            raise lost
        return {"id": cid}

    def comments(self, obj_id, token):
        return self.comments_by.get(obj_id, [])

    def reschedule_post(self, post_id, token, ts):
        p = self.posts[post_id]
        assert not p["published"], "уже опубликован"
        p["scheduled_ts"] = int(ts)
        return {"success": True}

    def delete(self, obj_id, token):
        for pid, p in list(self.posts.items()):
            if obj_id in (pid, p["photo_id"]):
                del self.posts[pid]
                return {"success": True}
        raise FacebookError("not found", code=100)

    def get(self, path, token, **params):
        pid = path.split("/")[0]
        p = self.cfg.page(pid)
        if p:
            return {"id": pid, "name": p.name}
        raise FacebookError("not found", code=100)


class FakeWP:
    def __init__(self):
        self.media, self.posts, self.fail_next = {}, {}, []
        self.calls = []

    def _maybe_fail(self, method):
        for i, (m, err) in enumerate(self.fail_next):
            if m == method:
                del self.fail_next[i]
                raise err

    def me(self):
        return {"id": 1, "name": "admin", "roles": ["administrator"], "capabilities": {"publish_posts": True}}

    def upload_media(self, data, filename, mime, title="", alt=""):
        self._maybe_fail("upload_media")
        mid = len(self.media) + 101
        self.media[mid] = filename
        return {"id": mid, "source_url": f"https://interes-world.com/wp-content/uploads/{filename}"}

    def create_post(self, title, content, status="draft", date_gmt=None, featured_media=None, categories=None,
                    slug=None):
        self.calls.append(("create_post", title, status))
        lost = None
        try:
            self._maybe_fail("create_post")
        except WordPressError as err:
            if not getattr(err, "after_create", False):
                raise
            lost = err
        pid = len(self.posts) + 5001
        self.posts[pid] = {"id": pid, "title": title, "content": content, "status": status, "date_gmt": date_gmt,
                           "slug": slug}
        if lost:
            raise lost
        return dict(self.posts[pid])

    def get_post(self, post_id):
        return dict(self.posts[post_id])

    def update_post(self, post_id, **fields):
        self.posts[post_id].update(fields)
        return dict(self.posts[post_id])

    def delete_post(self, post_id, force=True):
        return self.posts.pop(post_id)

    def delete_media(self, media_id):
        return self.media.pop(media_id, None)

    def find_by_slug(self, slug):
        self._maybe_fail("find_by_slug")
        return [dict(p) for p in self.posts.values() if p.get("slug") == slug]

    def update_media(self, media_id, **fields):
        if "slug" in fields:
            ext = self.media[media_id].rsplit(".", 1)[-1]
            self.media[media_id] = f"{fields['slug']}.{ext}"
        return {"id": media_id}

    def find_media_by_slug(self, slug):
        return [{"id": mid, "source_url": f"https://interes-world.com/wp-content/uploads/{fn}"}
                for mid, fn in self.media.items() if fn.rsplit(".", 1)[0] == slug]


def wp_error(status=500, msg="Internal error"):
    return WordPressError(msg, status)
