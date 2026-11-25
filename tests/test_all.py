"""Проверки проекта. Запуск: python -m unittest discover -s tests -v"""
import json
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from autopost import config as config_mod
from autopost.cli import cmd_private_test, cmd_public_test
from autopost.fakes import FakeGraph, FakeHTTP, FakeWP
from autopost.fb import FacebookError, Graph
from autopost.generate import Generator, detect_language, sanitize_article, validate
from autopost.pipeline import Pipeline
from autopost.simulate import STORY, run_simulation
from autopost.sources import SourceError, parse_link
from autopost.state import State
from autopost.tokens import TokenError, load_tokens, save_tokens
from autopost.workflow import workflow_in_sync

ROOT = Path(__file__).resolve().parent.parent


def _fixture_root() -> Path:
    """Тесты идут на постоянном наборе страниц (tests/fixture/config), а не на рабочем config/:
    рабочий список страниц этой системы меняется, а тесты проверяют логику."""
    fix = Path(tempfile.mkdtemp(prefix="autopost-fixture-"))
    shutil.copytree(ROOT / "tests" / "fixture" / "config", fix / "config")
    for d in ("prompts", ".github"):
        shutil.copytree(ROOT / d, fix / d)
    return fix


FIX = _fixture_root()

# Точные назначения из Make blueprint (модули 100–403): язык, page_id, название.
MAKE_PAGES = [
    ("FR", "1202025962997153", "Au Cœur des Vies"), ("FR", "1250374694822508", "Le Fil de la Vie"),
    ("FR", "1079295351933311", "Destins Touchants"), ("FR", "1145306638657445", "Larmes de Vie"),
    ("FR", "1032441206625600", "La Vie Telle Qu’elle Est"), ("FR", "1011074408765807", "Émotions Virales | Paris"),
    ("FR", "106591088255968", "Soleil et Amour"),
    ("IT", "1142735408923607", "Quand Tout Bascule"), ("IT", "1160223747182678", "Vite in Primo Piano"),
    ("IT", "997772916762884", "Storie Vere | Lion"), ("IT", "1079964265201139", "Voci di Vita | Paris"),
    ("IT", "1064982806700854", "Racconti Quotidiani | Monaco"), ("IT", "1140835409106424", "Vita Virale | Paris"),
    ("FR", "1058598924006917", "Moments Français | Paris"), ("IT", "1114015015119414", "Frammenti di Vita"),
    ("IT", "1030877436782212", "Una Storia che Ti Toccherà"),
    ("EN", "1235940142932696", "The Daily Report"), ("EN", "1167352663135230", "Human Life Stories"),
    ("EN", "1165836896614851", "Behind Her Smile"), ("EN", "914823471720372", "Life’s Echo"),
    ("EN", "921611111039858", "Beyond the Mask"), ("EN", "1016419758218222", "Whispers of Fate"),
    ("EN", "975714512286925", "Heartfelt Lessons"), ("EN", "236129656261330", "The Golden Touch"),
    ("EN", "104239205742669", "Amazing"), ("EN", "220697507802568", "Pet Lover"),
    ("EN", "117220714573166", "Best Stories"), ("EN", "162144310308183", "Sweet Paws #1"),
    ("EN", "108881571809093", "Sweet Paws #2"), ("EN", "237939756073256", "Love This World"),
    ("EN", "1164984823367955", "Fates Behind Closed Doors"), ("EN", "621364207735841", "Beautiful Babies"),
    ("NL", "633031599904551", "Het is interessant"), ("NL", "715935674934966", "Mooie kinderen"),
]


def temp_cfg():
    tmp = Path(tempfile.mkdtemp(prefix="autopost-test-"))
    for d in ("config", "prompts", ".github"):
        shutil.copytree(FIX / d, tmp / d)
    (tmp / "state").mkdir()
    cfg = config_mod.load(tmp)
    cfg.settings["slots"] = ["09:00", "12:00", "15:00", "18:00", "21:00"]  # фиксированные часы для тестов
    # тесты конвейера моделируют авто-режим без сети (таблица проверяется отдельным тестом)
    cfg.settings["sources"]["auto"]["enabled"] = True
    cfg.settings["sources"]["google_sheet"]["enabled"] = False
    return cfg, tmp


class ConfigTests(unittest.TestCase):
    def test_exact_page_assignments(self):
        cfg = config_mod.load(FIX)
        got = [(p.lang, p.page_id, p.name) for p in cfg.all_pages if p.from_make]
        self.assertEqual(got, MAKE_PAGES)
        # отключённые страницы (disabled: true) не публикуются
        self.assertTrue(all(not p.disabled for p in cfg.pages))
        self.assertEqual(len(cfg.pages) + sum(p.disabled for p in cfg.all_pages), len(cfg.all_pages))
        self.assertEqual(config_mod.check_expected_counts(cfg), [])

    def test_workflow_matches_slots(self):
        self.assertTrue(workflow_in_sync(config_mod.load(ROOT)),
                        "расписание в autopost.yml устарело — выполните python -m autopost render-workflow")


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.map = config_mod.load(FIX).profile_to_page()

    def test_photo_link_with_profile_id(self):
        r = parse_link("https://www.facebook.com/photo.php?fbid=122100&set=pb.61592252135792.-2207520000&type=3",
                       self.map)
        self.assertEqual(r["page_id"], "1235940142932696")  # The Daily Report
        self.assertEqual(r["part"], "122100")

    def test_permalink(self):
        r = parse_link("https://www.facebook.com/permalink.php?story_fbid=555&id=61577900372290", self.map)
        self.assertEqual((r["page_id"], r["part"]), ("633031599904551", "555"))

    def test_posts_and_raw(self):
        self.assertEqual(parse_link("https://facebook.com/1202025962997153/posts/777?x=1", self.map)["part"], "777")
        self.assertEqual(parse_link("1202025962997153_888", self.map)["candidates"][0], "1202025962997153_888")

    def test_short_link_rejected(self):
        with self.assertRaises(SourceError):
            parse_link("https://www.facebook.com/share/p/1ABCdef/", self.map)


class TextTests(unittest.TestCase):
    def test_validation(self):
        src = "word " * 200
        article = "<p>" + "Elle ne savait pas que ce jour-là allait changer sa vie. " * 60 + "</p>"
        fb_ok = "Elle ne savait pas que la vie est belle et que tout change. " * 16
        errs, _ = validate("FR", src, "Titre", fb_ok, article)
        self.assertEqual(errs, [])
        errs, _ = validate("FR", src, "Titre", fb_ok + " https://x.y", article)
        self.assertTrue(any("ссылка" in e for e in errs))
        errs, _ = validate("IT", src, "Titolo", fb_ok, article)
        self.assertTrue(any("язык" in e for e in errs))

    def test_sanitize(self):
        out = sanitize_article('<div><p>Hi <a href="http://x">there</a></p><img src="x"><script>x</script></div>')
        self.assertEqual(out, "<p>Hi there</p>")

    def test_detect(self):
        self.assertEqual(detect_language("Lei non sapeva che quel giorno le avrebbe cambiato la vita"), "IT")
        self.assertEqual(detect_language("Ze wist niet dat die dag haar leven zou veranderen"), "NL")


class TokenTests(unittest.TestCase):
    def test_roundtrip_and_wrong_secret(self):
        cfg, _ = temp_cfg()
        save_tokens(cfg, "s3cret", {"1": "tok"}, {})
        self.assertEqual(load_tokens(cfg, "s3cret"), {"1": "tok"})
        self.assertNotIn(b"tok", cfg.path("state/page_tokens.enc").read_bytes())
        with self.assertRaises(TokenError):
            load_tokens(cfg, "other")

    def test_two_users_split_pages_and_owners_saved(self):
        from autopost.tokens import setup_facebook, token_origins
        cfg, _ = temp_cfg()
        graph = FakeGraph(cfg, lambda: None)
        ids = [p.page_id for p in cfg.pages]
        first, second = set(ids[:3]), set(ids[3:])
        full = graph.my_pages

        def mine(user_token):
            want = first if "U1" in user_token else second
            return [p for p in full(user_token) if p["id"] in want]
        graph.my_pages = mine
        rep = setup_facebook(cfg, graph, "app", "s3cret", ["U1", "U2"])
        self.assertEqual(rep["per_user"], [len(first), len(second)])
        o = token_origins(cfg, "s3cret")
        self.assertEqual({k for k, v in o.items() if v == "autopost2"}, first)
        self.assertEqual({k for k, v in o.items() if v == "autopost3"}, second)

    def test_extra_token_file_fills_only_missing_pages(self):
        from autopost.tokens import _fernet, token_origins
        cfg, _ = temp_cfg()
        save_tokens(cfg, "s3cret", {"1": "main1", "2": "main2"}, {})
        blob = json.dumps({"tokens": {"2": "old2", "3": "old3"}, "meta": {}}).encode()
        cfg.path("state/old.enc").write_bytes(_fernet("s3cret").encrypt(blob))
        self.assertEqual(load_tokens(cfg, "s3cret"), {"1": "main1", "2": "main2"})   # без настройки — как раньше
        cfg.settings.setdefault("facebook", {})["extra_token_files"] = ["state/old.enc", "state/missing.enc"]
        self.assertEqual(load_tokens(cfg, "s3cret"), {"1": "main1", "2": "main2", "3": "old3"})
        self.assertEqual(token_origins(cfg, "s3cret"), {"1": "main", "2": "main", "3": "state/old.enc"})
        save_tokens(cfg, "s3cret", {"1": "main1", "2": "main2"}, {"owners": {"1": "autopost2", "2": "autopost3"}})
        self.assertEqual(token_origins(cfg, "s3cret"), {"1": "autopost2", "2": "autopost3", "3": "state/old.enc"})


class GraphClientTests(unittest.TestCase):
    class Sess:
        def __init__(self, responses):
            self.responses, self.n = list(responses), 0

        def _next(self, *a, **k):
            self.n += 1
            code, body = self.responses.pop(0)
            return SimpleNamespace(status_code=code, json=lambda: body, text=json.dumps(body))

        get = post = delete = _next

    def test_error_parsing_and_retry_rules(self):
        s = self.Sess([(500, {"error": {"message": "x", "code": 2}}), (200, {"id": "1"})])
        g = Graph(session=s, backoff=0)
        self.assertEqual(g.get("me", "t"), {"id": "1"})  # GET повторяется
        s = self.Sess([(500, {"error": {"message": "x", "code": 2}}), (200, {"id": "1"})])
        g = Graph(session=s, backoff=0)
        with self.assertRaises(FacebookError):
            g.post("p/photos", "t")  # POST не повторяется вслепую
        self.assertEqual(s.n, 1)
        s = self.Sess([(400, {"error": {"message": "expired", "code": 190}})])
        with self.assertRaises(FacebookError) as cm:
            Graph(session=s, backoff=0).get("me", "t")
        self.assertTrue(cm.exception.auth)


class PipelineTests(unittest.TestCase):
    def test_simulation_one_day(self):
        ok, md = run_simulation(config_mod.load(FIX), days=1)
        self.assertTrue(ok, md)

    def _setup(self, enabled_at):
        cfg, tmp = temp_cfg()
        cfg.settings["schedule_enabled"] = True
        tz = ZoneInfo(cfg.tz)
        clock = {"now": enabled_at.replace(tzinfo=tz)}
        graph, wp = FakeGraph(cfg, lambda: clock["now"]), FakeWP()
        st = State(tmp / "state/state.json")
        st.data["source_cutoff"] = (clock["now"] - timedelta(days=1)).isoformat()
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        page = cfg.pages[0]
        graph.source_posts[page.page_id] = [{
            "id": f"{page.page_id}_42", "message": STORY, "full_picture": "https://scontent.example/x.jpg",
            "attachments": {"data": [{"media_type": "photo"}]}, "_ts": int(clock["now"].timestamp()) - 3600}]

        def run():
            return Pipeline(cfg, st, graph, wp, Generator(cfg, backend="mock"), tokens,
                            now_fn=lambda: clock["now"], http=FakeHTTP()).tick()
        return cfg, clock, graph, wp, st, run

    def test_late_slot_published_immediately_with_comment(self):
        cfg, clock, graph, wp, st, run = self._setup(datetime(2030, 5, 1, 8, 50))
        run()  # 08:50 — подготовка успевает, но ставить в отложенные уже поздно (< 15 мин)
        self.assertEqual(len(graph.posts), 0)
        clock["now"] = clock["now"].replace(hour=9, minute=3)
        rep = run()
        self.assertEqual(rep.final_errors, [])
        self.assertEqual(len(graph.posts), len(cfg.pages) - 1)
        self.assertTrue(all(not p["scheduled_ts"] for p in graph.posts.values()))
        self.assertEqual(sum(len(v) for v in graph.comments_by.values()), len(cfg.pages) - 1)
        self.assertEqual(st.slots["2030-05-01 09:00"]["status"], "done")

    def test_parts_format_post_comment_article(self):
        """Формат «по частям»: пост — заголовок + часть 1; комментарий — ЧАСТЬ 2 … ЧАСТЬ 3 + ссылка; без дублей."""
        cfg, clock, graph, wp, st, run = self._setup(datetime(2030, 5, 1, 6, 7))
        cfg.settings["post_format"] = "parts"
        run()                                            # 06:07 — подготовка и отложенные посты на 09:00
        posts = list(graph.posts.values())
        self.assertTrue(posts)
        it_page = cfg.pages_for("IT")[0].page_id
        p = next(x for x in posts if x["page"] == it_page)
        self.assertIn("Grazie per aver letto fin qui.", p["message"])
        self.assertTrue(p["message"].startswith("Un incontro che ha cambiato tutto\n\n"))
        self.assertTrue(any("<h2>PARTE 3</h2>" in w.get("content", "") for w in wp.posts.values()))
        clock["now"] = clock["now"].replace(hour=9, minute=7)
        run()
        clock["now"] = clock["now"].replace(hour=9, minute=37)
        run()                                            # повторный запуск — комментарий не дублируется
        for pid, cms in graph.comments_by.items():
            self.assertEqual(len(cms), 1)
            msg = cms[0]["message"]
            self.assertTrue(msg.split("\n")[0].endswith(" 2"), msg[:40])
            self.assertRegex(msg, r"\n\S+ 3\nhttps?://\S+$")
        self.assertEqual(sum(len(v) for v in graph.comments_by.values()), len(cfg.pages) - 1)

    def test_comment_falls_back_without_outro(self):
        """Facebook отклоняет комментарий со строкой «лайк и „Да“» (FB 1/1446042) — ставим тот же текст без неё."""
        from autopost.fb import FacebookError
        cfg, clock, graph, wp, st, run = self._setup(datetime(2030, 5, 1, 6, 7))
        cfg.settings["post_format"] = "parts"
        orig = graph.comment

        def picky(obj_id, token, message):
            if "Grazie per aver letto fino alla fine" in message:
                raise FacebookError("An unknown error occurred", code=1, subcode=1446042)
            return orig(obj_id, token, message)
        graph.comment = picky
        run()
        clock["now"] = clock["now"].replace(hour=9, minute=7)
        run()
        it_ids = {p.page_id for p in cfg.pages_for("IT")}
        it_key = next(k for k in graph.comments_by if k.split("_")[0] in it_ids)
        msgs = [c["message"] for c in graph.comments_by[it_key]]
        self.assertEqual(len(msgs), 1)
        self.assertNotIn("Grazie per aver letto fino alla fine", msgs[0])
        self.assertIn("PARTE 3", msgs[0])
        self.assertEqual(sum(len(v) for v in graph.comments_by.values()), len(cfg.pages) - 1)

    def test_comment_last_resort_link_only(self):
        """Facebook отклоняет любой длинный текст части 2 — ставим «ЧАСТЬ 3» + ссылку."""
        from autopost.fb import FacebookError
        cfg, clock, graph, wp, st, run = self._setup(datetime(2030, 5, 1, 6, 7))
        cfg.settings["post_format"] = "parts"
        orig = graph.comment

        def picky(obj_id, token, message):
            if len(message) > 300:
                raise FacebookError("An unknown error occurred", code=1, subcode=1446042)
            return orig(obj_id, token, message)
        graph.comment = picky
        run()
        clock["now"] = clock["now"].replace(hour=9, minute=7)
        run()
        msgs = [c["message"] for v in graph.comments_by.values() for c in v]
        self.assertEqual(len(msgs), len(cfg.pages) - 1)
        self.assertTrue(all(re.match(r"^\S+ 3\nhttps?://\S+$", m) for m in msgs), msgs[:2])

    def test_post_format_by_group(self):
        cfg = config_mod.load(FIX)
        cfg.settings["post_format"] = "classic"
        cfg.settings["post_format_by_group"] = {"IT": "parts", "RU": "parts"}
        got = {g: Generator(config_mod.group_view(cfg, g), backend="mock").post_format for g in cfg.groups()}
        self.assertEqual({g for g, f in got.items() if f == "parts"}, {"IT", "RU"})

    def test_no_source_is_not_fatal(self):
        cfg, clock, graph, wp, st, run = self._setup(datetime(2030, 5, 1, 6, 0))
        graph.source_posts.clear()
        clock["now"] = clock["now"].replace(hour=6, minute=3)
        rep = run()
        self.assertEqual(rep.final_errors, [])
        self.assertEqual(st.slots["2030-05-01 09:00"]["status"], "no_source")
        self.assertEqual(len(graph.posts), 0)

    def test_disabled_schedule_does_nothing(self):
        cfg, clock, graph, wp, st, run = self._setup(datetime(2030, 5, 1, 6, 0))
        cfg.settings["schedule_enabled"] = False
        run()
        self.assertEqual((len(graph.posts), len(wp.posts)), (0, 0))


class ResilienceTests(PipelineTests):
    """Сценарии из независимой проверки: потеря состояния, «прошедшие» ошибки, нет токена, утечки."""

    def _slot_run(self, start=datetime(2030, 5, 1, 6, 7)):
        cfg, clock, graph, wp, st, run = self._setup(start)
        return cfg, clock, graph, wp, st, run

    def test_state_loss_does_not_duplicate(self):
        import copy
        cfg, clock, graph, wp, st, run = self._slot_run()
        before = copy.deepcopy(st.data)
        run()                                     # 06:07 — подготовка и отложенные посты на 09:00
        self.assertEqual((len(graph.posts), len(wp.posts)), (len(cfg.pages) - 1, len(cfg.active_langs())))
        st.data = before                          # «потеряли» state.json (тексты в content/ остались)
        clock["now"] = clock["now"].replace(hour=8, minute=7)
        run()
        self.assertEqual((len(graph.posts), len(wp.posts)), (len(cfg.pages) - 1, len(cfg.active_langs())))
        st.data = copy.deepcopy(before)           # и ещё раз, уже после выхода
        st.data["source_cutoff"] = before["source_cutoff"]
        clock["now"] = clock["now"].replace(hour=9, minute=7)
        run()
        self.assertEqual((len(graph.posts), len(wp.posts)), (len(cfg.pages) - 1, len(cfg.active_langs())))
        self.assertEqual(sum(len(v) for v in graph.comments_by.values()), len(cfg.pages) - 1)

    def test_comment_error_that_went_through(self):
        cfg, clock, graph, wp, st, run = self._slot_run()
        run()
        target_page = cfg.pages_for("IT")[0].page_id
        err = FacebookError("An unexpected error has occurred", code=2)
        err.after_create = True
        graph.fail_next.append(("comment", target_page, err))
        clock["now"] = clock["now"].replace(hour=9, minute=7)
        run()
        clock["now"] = clock["now"].replace(hour=9, minute=37)
        run()
        self.assertTrue(all(len(graph.comments_by.get(pid, [])) == 1 for pid in graph.posts))

    def test_wp_lookup_failure_does_not_create(self):
        from autopost.wp import WordPressError
        cfg, clock, graph, wp, st, run = self._slot_run()
        wp.fail_next += [("find_by_slug", WordPressError("timeout", None))] * len(cfg.active_langs())
        run()
        self.assertEqual(len(wp.posts), 0)
        clock["now"] = clock["now"].replace(hour=8, minute=7)
        run()
        self.assertEqual(len(wp.posts), len(cfg.active_langs()))

    def test_wp_502_after_save_no_duplicate(self):
        from autopost.wp import WordPressError
        cfg, clock, graph, wp, st, run = self._slot_run()
        err = WordPressError("Bad gateway", 502)
        err.after_create = True
        wp.fail_next.append(("create_post", err))
        run()
        clock["now"] = clock["now"].replace(hour=8, minute=7)
        run()
        self.assertEqual(len(wp.posts), len(cfg.active_langs()))

    def test_one_page_without_token_does_not_stop_others(self):
        cfg, clock, graph, wp, st, run = self._slot_run()
        missing = cfg.pages_for("EN")[3].page_id
        run.__closure__  # noqa: B018
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages if p.page_id != missing}
        rep = Pipeline(cfg, st, graph, wp, Generator(cfg, backend="mock"), tokens,
                       now_fn=lambda: clock["now"], http=FakeHTTP()).tick()
        self.assertEqual(len(graph.posts), len(cfg.pages) - 2)
        self.assertEqual(rep.final_errors, [])

    def test_unconnected_pages_are_skipped_quietly(self):
        """11 страниц ещё не подключены: остальные публикуются, слот закрывается без ошибок и красных запусков."""
        cfg, clock, graph, wp, st, run = self._slot_run()
        missing = {p.page_id for p in cfg.pages[1::3]}  # pages[0] — источник истории
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages if p.page_id not in missing}
        reports = []
        for hh, mm in ((6, 7), (8, 7), (9, 7), (9, 37), (10, 7), (11, 7)):
            clock["now"] = clock["now"].replace(hour=hh, minute=mm)
            reports.append(Pipeline(cfg, st, graph, wp, Generator(cfg, backend="mock"), tokens,
                                    now_fn=lambda: clock["now"], http=FakeHTTP()).tick())
        self.assertEqual([e for r in reports for e in r.final_errors], [])
        # предупреждения только о пустой очереди следующих слотов (в тесте одна история), не о страницах
        self.assertEqual([e for r in reports for e in r.errors if "истори" not in e], [])
        self.assertTrue(all(graph.posts[pid]["page"] not in missing for pid in graph.posts))
        self.assertEqual(sum(len(v) for v in graph.comments_by.values()), len(graph.posts))
        self.assertEqual(st.slots["2030-05-01 09:00"]["status"], "done")

    def test_language_without_connected_pages_is_skipped(self):
        """Обе страницы NL не подключены: NL-текст и NL-статья не создаются, остальные языки выходят."""
        cfg, clock, graph, wp, st, run = self._slot_run()
        nl = {p.page_id for p in cfg.pages_for("NL")}
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages if p.page_id not in nl}
        reports = []
        for hh, mm in ((6, 7), (8, 7), (9, 7), (9, 37), (10, 7), (11, 7)):
            clock["now"] = clock["now"].replace(hour=hh, minute=mm)
            reports.append(Pipeline(cfg, st, graph, wp, Generator(cfg, backend="mock"), tokens,
                                    now_fn=lambda: clock["now"], http=FakeHTTP()).tick())
        self.assertEqual(len(wp.posts), len(cfg.active_langs()) - 1)
        self.assertEqual([e for r in reports for e in r.final_errors], [])
        slot = st.slots["2030-05-01 09:00"]
        self.assertEqual(slot["status"], "done")
        self.assertTrue(slot["langs"]["NL"].get("skipped"))
        self.assertEqual(len(graph.posts), len(cfg.pages) - 1 - len(nl))

    def test_tokens_are_redacted(self):
        from autopost.fb import redact
        e = FacebookError("сеть: https://graph.facebook.com/v26.0/me?access_token=EAAB123&x=1 client_secret=abc")
        self.assertNotIn("EAAB123", str(e))
        self.assertNotIn("abc", redact("client_secret=abc"))


class TestCommands(unittest.TestCase):
    def test_private_then_public(self):
        cfg, tmp = temp_cfg()
        clock = lambda: datetime.now(ZoneInfo(cfg.tz))  # noqa: E731
        graph, wp, http = FakeGraph(cfg, clock), FakeWP(), FakeHTTP()
        page = cfg.pages[5]
        graph.source_posts[page.page_id] = [{
            "id": f"{page.page_id}_77", "message": STORY, "full_picture": "https://scontent.example/x.jpg",
            "attachments": {"data": [{"media_type": "photo"}]}, "_ts": int(clock().timestamp()) - 600}]
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        args = SimpleNamespace(source="", page="", keep=False, backend="mock")
        self.assertEqual(cmd_private_test(cfg, args, services=(graph, wp, http), tokens=tokens), 0)
        self.assertEqual(len(graph.posts), 0, "закрытый тест не должен оставлять постов")
        self.assertEqual(len(wp.posts), 0, "черновик удалён")
        preview = sorted(cfg.path("previews").glob("test-*.md"))[-1].read_text(encoding="utf-8")
        self.assertIn("Het is interessant", preview)
        args = SimpleNamespace(test_id="", page="", confirm="no")
        self.assertEqual(cmd_public_test(cfg, args, services=(graph, wp, http), tokens=tokens), 1)
        args.confirm = "PUBLISH"
        self.assertEqual(cmd_public_test(cfg, args, services=(graph, wp, http), tokens=tokens), 0)
        self.assertEqual(len(graph.posts), 1)
        (pid, post), = graph.posts.items()
        self.assertEqual(post["page"], cfg.test_page_id())
        self.assertTrue(graph.comments_by[pid][0]["message"].startswith(cfg.site["comment_link_format"].split("{")[0]))
        st = State(cfg.path("state/state.json"))
        self.assertEqual(st.sources[f"post:{page.page_id}_77"]["status"], "used")


if __name__ == "__main__":
    unittest.main()


class SheetQueueTests(unittest.TestCase):
    """Как watchRows в Make: старые строки таблицы не публикуются, новые берутся по порядку, каждая один раз."""

    def test_baseline_then_new_rows(self):
        from unittest import mock
        from autopost import sources
        cfg, tmp = temp_cfg()
        cfg.settings["sources"]["auto"]["enabled"] = False
        cfg.settings["sources"]["google_sheet"]["enabled"] = True
        now = datetime(2030, 5, 1, 6, 0, tzinfo=ZoneInfo(cfg.tz))
        graph = FakeGraph(cfg, lambda: now)
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        pid = cfg.pages[0].page_id
        for n in (1, 2, 3):
            graph.source_posts.setdefault(pid, []).append({
                "id": f"{pid}_{n}", "message": STORY + f" #{n}", "full_picture": "https://x/y.jpg",
                "attachments": {"data": [{"media_type": "photo"}]}})
        st = State(tmp / "state" / "state.json")
        rows = [f"{pid}_1"]                       # старая ссылка, была в таблице до подключения
        with mock.patch.object(sources, "google_sheet_links", lambda cfg: list(rows)):
            self.assertIsNone(sources.pick_source(cfg, graph, tokens, st, now, print))
            rows += [f"{pid}_2", f"{pid}_3"]      # новые строки
            first = sources.pick_source(cfg, graph, tokens, st, now, print)
            self.assertEqual(first["key"], f"post:{pid}_2")
            st.mark_source(first["key"], status="used")
            second = sources.pick_source(cfg, graph, tokens, st, now, print)
            self.assertEqual(second["key"], f"post:{pid}_3")
            st.mark_source(second["key"], status="used")
            self.assertIsNone(sources.pick_source(cfg, graph, tokens, st, now, print))


class CancelSlotTests(PipelineTests):
    def test_cancel_prepared_slot(self):
        from autopost.cli import cmd_cancel_slot
        cfg, clock, graph, wp, st, run = self._setup(datetime(2030, 5, 1, 6, 7))
        run()
        scheduled = [p for p in graph.posts.values() if not p["published"]]
        self.assertEqual(len(scheduled), len(cfg.pages) - 1)
        self.assertEqual(len(wp.posts), len(cfg.active_langs()))
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        rc = cmd_cancel_slot(cfg, "2030-05-01 09:00", services=(graph, wp, FakeHTTP()), tokens=tokens)
        self.assertEqual(rc, 0)
        self.assertEqual((len(graph.posts), len(wp.posts)), (0, 0))
        clock["now"] = clock["now"].replace(hour=9, minute=7)
        run()                                  # после отмены слот больше не трогается
        self.assertEqual((len(graph.posts), len(wp.posts)), (0, 0))


class GroupQueueTests(unittest.TestCase):
    """У каждой группы своя очередь и своя история: посты только на своих страницах, свои статьи и фото."""

    def test_groups_get_own_stories(self):
        from autopost.config import group_view
        cfg, tmp = temp_cfg()
        cfg.settings["schedule_enabled"] = True
        clock = {"now": datetime(2030, 5, 1, 6, 7, tzinfo=ZoneInfo(cfg.tz))}
        graph, wp = FakeGraph(cfg, lambda: clock["now"]), FakeWP()
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        groups = cfg.groups()
        self.assertEqual(groups, ["FR", "IT", "EN", "EN2", "NL", "RU", "MIX"])
        self.assertEqual(cfg.group_label("MIX"), "Mix (RU+ES+RO+AR)")
        self.assertEqual(cfg.group_label("RU"), "RU+TR+PT")
        self.assertEqual(cfg.group_label("NL"), "NL+DE")
        self.assertEqual(cfg.group_label("IT"), "IT+PL+CS")
        n_articles = sum(len(cfg.group_langs(g)) for g in groups)   # в группе из нескольких языков — статья на язык
        # «чужая» страница-источник для каждой группы — истории читаются через запасную страницу
        fb = cfg.fallback_source_page_id
        (tmp / "queue").mkdir(exist_ok=True)
        for i, g in enumerate(groups):
            pid = cfg.pages[0].page_id
            graph.source_posts.setdefault(pid, []).append({
                "id": f"{pid}_{900 + i}", "message": f"Story for {g}. " + STORY, "full_picture": "https://x/y.jpg",
                "attachments": {"data": [{"media_type": "photo"}]}})
            (tmp / "queue" / f"{g}.txt").write_text(f"# очередь {g}\n{pid}_{900 + i}\n", encoding="utf-8")
        states = {g: State(tmp / f"state/groups/{g}.json") for g in groups}

        def tick():
            for g in groups:
                gcfg = group_view(cfg, g)
                Pipeline(gcfg, states[g], graph, wp, Generator(gcfg, backend="mock"), tokens,
                         now_fn=lambda: clock["now"], http=FakeHTTP()).tick()

        for hh, mm in ((6, 7), (8, 7), (9, 7), (9, 37)):
            clock["now"] = clock["now"].replace(hour=hh, minute=mm)
            tick()
            tick()  # повторный запуск не создаёт дублей
        slot = "2030-05-01 09:00"
        for g in groups:
            s = states[g].slots[slot]
            self.assertEqual(s["status"], "done", g)
            self.assertEqual(s["source"]["text"].split(".")[0], f"Story for {g}")
        src_page = cfg.pages[0].page_id
        expected = sum(1 for p in cfg.pages if p.page_id != src_page)
        self.assertEqual(len(graph.posts), expected)                 # каждая страница — один пост
        self.assertEqual(len({p["page"] for p in graph.posts.values()}), expected)
        self.assertEqual(len(wp.posts), n_articles)                  # своя статья у каждой группы и языка
        self.assertEqual(len({p["slug"] for p in wp.posts.values()}), n_articles)
        self.assertEqual(len(wp.media), len(groups))                 # и своё фото
        self.assertTrue(all(len(graph.comments_by.get(pid, [])) == 1 for pid in graph.posts))
        # следующий выпуск: очереди пусты — ничего не публикуется, ошибок нет
        clock["now"] = clock["now"].replace(hour=11, minute=7)
        tick()
        self.assertEqual(len(graph.posts), expected)


def _queue_story(graph, cfg, tmp, g, n, text):
    pid = cfg.pages[0].page_id   # «чужая» для групп RU/NL страница-источник
    graph.source_posts.setdefault(pid, []).append({
        "id": f"{pid}_{n}", "message": text + " " + STORY, "full_picture": "https://x/y.jpg",
        "attachments": {"data": [{"media_type": "photo"}]}})
    q = tmp / "queue" / f"{g}.txt"
    q.parent.mkdir(exist_ok=True)
    q.write_text((q.read_text(encoding="utf-8") if q.exists() else f"# {g}\n") + f"{pid}_{n}\n", encoding="utf-8")


class RegroupTests(unittest.TestCase):
    """Страницы перенесли в другую группу, когда выпуск уже подготовлен: прежняя группа дописывает
    комментарии к своим постам, новая не ставит этим страницам второй пост; со следующего выпуска — общая история."""

    def test_move_pages_between_groups_mid_flight(self):
        from autopost.cli import _legacy_groups
        from autopost.config import group_view
        cfg, tmp = temp_cfg()
        cfg.settings["schedule_enabled"] = True
        clock = {"now": datetime(2030, 5, 1, 6, 7, tzinfo=ZoneInfo(cfg.tz))}
        graph, wp = FakeGraph(cfg, lambda: clock["now"]), FakeWP()
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        tr = [p for p in cfg.pages if p.lang == "TR"]
        for p in tr:
            p.group = "TR"                       # как было до переноса
        _queue_story(graph, cfg, tmp, "RU", 901, "Story R1.")
        _queue_story(graph, cfg, tmp, "TR", 902, "Story T1.")

        def run(views):
            for g, (gcfg, st) in views.items():
                Pipeline(gcfg, st, graph, wp, Generator(gcfg, backend="mock"), tokens,
                         now_fn=lambda: clock["now"], http=FakeHTTP()).tick()

        st = {g: State(tmp / f"state/groups/{g}.json") for g in ("RU", "TR")}
        run({g: (group_view(cfg, g), st[g]) for g in ("RU", "TR")})
        self.assertTrue(st["TR"].slots["2030-05-01 09:00"]["prepared"])
        # перенос: TR теперь в группе RU; следующая история группы RU — уже на трёх языках
        for p in tr:
            p.group = "RU"
        _queue_story(graph, cfg, tmp, "RU", 903, "Story R2.")
        for hh, mm in ((9, 7), (9, 37), (12, 7), (12, 37)):
            clock["now"] = clock["now"].replace(hour=hh, minute=mm)
            legacy = _legacy_groups(cfg)
            views = {g: (group_view(cfg, g), State(tmp / f"state/groups/{g}.json")) for g in cfg.groups()
                     if g in ("RU",)}
            views.update({g: (group_view(cfg, g, page_ids=ids), lst) for g, (lst, ids) in legacy.items()})
            run(views)
        tr_ids = {p.page_id for p in tr}
        per_page = {}
        for pid, post in graph.posts.items():
            per_page.setdefault(post["page"], []).append(pid)
        for p in tr:
            self.assertEqual(len(per_page[p.page_id]), 2, p.name)    # 09:00 (прежняя группа) + 12:00 (RU)
            self.assertTrue(all(len(graph.comments_by.get(x, [])) == 1 for x in per_page[p.page_id]), p.name)
        old = State(tmp / "state/groups/TR.json")
        self.assertEqual(old.slots["2030-05-01 09:00"]["status"], "done")
        ru = State(tmp / "state/groups/RU.json")
        self.assertEqual(ru.slots["2030-05-01 09:00"]["status"], "done")
        self.assertTrue(ru.slots["2030-05-01 12:00"]["langs"]["TR"].get("generated"))
        self.assertTrue(all(ru.slots["2030-05-01 09:00"]["langs"]["TR"]["pages"][x]["status"] == "no_access"
                            for x in tr_ids))
        self.assertEqual(_legacy_groups(cfg), {})                    # прежней группе больше нечего дописывать


class SiteSwitchTests(unittest.TestCase):
    """Сайт группы сменили после подготовки выпуска: статья и ссылка в комментарии — со старого сайта."""

    def test_prepared_release_stays_on_its_site(self):
        from autopost.config import group_view
        cfg, tmp = temp_cfg()
        cfg.settings["schedule_enabled"] = True
        clock = {"now": datetime(2030, 5, 1, 6, 7, tzinfo=ZoneInfo(cfg.tz))}
        graph, wp_a, wp_b = FakeGraph(cfg, lambda: clock["now"]), FakeWP(), FakeWP()
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        _queue_story(graph, cfg, tmp, "NL", 911, "Story N1.")
        st = State(tmp / "state/groups/NL.json")
        url_a = cfg.site["url"]
        gcfg = group_view(cfg, "NL")
        Pipeline(gcfg, st, graph, wp_a, Generator(gcfg, backend="mock"), tokens,
                 now_fn=lambda: clock["now"], http=FakeHTTP()).tick()
        self.assertEqual(len(wp_a.posts), len(cfg.group_langs("NL")))   # статья на каждый язык группы
        cfg.settings["group_sites"] = {"NL": {"url": "https://b.example",
                                              "comment_link_format": "https://b.example/?p={wp_id}"}}
        made = []

        def factory(site):
            made.append(site["url"])
            return wp_a if site["url"] == url_a else None
        for hh, mm in ((9, 7), (9, 37)):
            clock["now"] = clock["now"].replace(hour=hh, minute=mm)
            gcfg = group_view(cfg, "NL")
            self.assertEqual(gcfg.site["url"], "https://b.example")
            Pipeline(gcfg, st, graph, wp_b, Generator(gcfg, backend="mock"), tokens,
                     now_fn=lambda: clock["now"], http=FakeHTTP(), wp_factory=factory).tick()
        self.assertEqual(wp_b.posts, {})
        self.assertIn(url_a, made)
        self.assertTrue(all(p["status"] == "publish" for p in wp_a.posts.values()))
        prefix = cfg.settings["site"]["comment_link_format"].split("{")[0]
        msgs = [c["message"] for cs in graph.comments_by.values() for c in cs]
        self.assertTrue(msgs and all(m.startswith(prefix) for m in msgs), msgs[:2])
        self.assertEqual(st.slots["2030-05-01 09:00"]["status"], "done")


class ReworkSlotTests(unittest.TestCase):
    """Подготовленный выпуск переделывается по новой настройке: новый язык группы и новый сайт."""

    def test_rework_adds_language_and_moves_site(self):
        from autopost.cli import rework_slot
        from autopost.config import group_view
        cfg, tmp = temp_cfg()
        cfg.settings["schedule_enabled"] = True
        clock = {"now": datetime(2030, 5, 1, 6, 7, tzinfo=ZoneInfo(cfg.tz))}
        graph, wp_a, wp_b = FakeGraph(cfg, lambda: clock["now"]), FakeWP(), FakeWP()
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        de = [p for p in cfg.pages if p.lang == "DE"]
        nl = [p for p in cfg.pages if p.lang == "NL"]
        for p in de:
            p.group = "DE"                        # до переноса группа NL — только NL
        _queue_story(graph, cfg, tmp, "NL", 921, "Story N1.")
        url_a = cfg.site["url"]
        st_path = tmp / "state/groups/NL.json"
        gcfg = group_view(cfg, "NL")
        Pipeline(gcfg, State(st_path), graph, wp_a, Generator(gcfg, backend="mock"), tokens,
                 now_fn=lambda: clock["now"], http=FakeHTTP()).tick()
        n_before = len(cfg.group_langs("NL"))
        self.assertEqual(len(wp_a.posts), n_before)
        first_posts = dict(graph.posts)
        self.assertEqual({p["page"] for p in first_posts.values()},
                         {p.page_id for p in cfg.pages if p.group == "NL"})
        # новая настройка: DE в группе NL, у группы другой сайт
        for p in de:
            p.group = "NL"
        cfg.settings["group_sites"] = {"NL": {"url": "https://b.example",
                                              "comment_link_format": "https://b.example/?p={wp_id}"}}
        factory = lambda site: wp_a if site["url"] == url_a else wp_b   # noqa: E731
        res = rework_slot(cfg, "NL", "2030-05-01 09:00", move_site=True, wp_factory=factory)
        self.assertIn("DE", res)
        for hh, mm in ((6, 20), (9, 7), (9, 37)):
            clock["now"] = clock["now"].replace(hour=hh, minute=mm)
            gcfg = group_view(cfg, "NL")
            Pipeline(gcfg, State(st_path), graph, wp_b, Generator(gcfg, backend="mock"), tokens,
                     now_fn=lambda: clock["now"], http=FakeHTTP(), wp_factory=factory).tick()
        self.assertEqual(wp_a.posts, {})                              # старая статья удалена
        self.assertEqual(len(wp_b.posts), n_before + 1)               # все языки группы + DE на новом сайте
        self.assertEqual(len({p["slug"] for p in wp_b.posts.values()}), n_before + 1)
        new_posts = {k: v for k, v in graph.posts.items() if k not in first_posts}
        self.assertEqual({p["page"] for p in new_posts.values()}, {p.page_id for p in de})  # NL — без вторых постов
        msgs = [c["message"] for cs in graph.comments_by.values() for c in cs]
        self.assertEqual(len(msgs), len(graph.posts))
        self.assertTrue(all(m.startswith("https://b.example/?p=") for m in msgs), msgs[:3])
        self.assertEqual(State(st_path).slots["2030-05-01 09:00"]["status"], "done")


class DropLangTests(unittest.TestCase):
    def test_drop_lang_then_rework_switches_pages_to_other_language(self):
        """Страницы DE в группе NL переходят на NL: посты DE удаляются, вместо них — NL, без дублей."""
        from autopost.cli import drop_lang, rework_slot
        from autopost.config import group_view
        cfg, tmp = temp_cfg()
        cfg.settings["schedule_enabled"] = True
        clock = {"now": datetime(2030, 5, 1, 6, 7, tzinfo=ZoneInfo(cfg.tz))}
        graph, wp = FakeGraph(cfg, lambda: clock["now"]), FakeWP()
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        de = [p for p in cfg.pages if p.lang == "DE"]
        for p in de:
            p.group = "NL"
        _queue_story(graph, cfg, tmp, "NL", 941, "Story D1.")
        st_path = tmp / "state/groups/NL.json"
        services = (graph, wp, FakeHTTP())

        def tick():
            gcfg = group_view(cfg, "NL")
            Pipeline(gcfg, State(st_path), graph, wp, Generator(gcfg, backend="mock"), tokens,
                     now_fn=lambda: clock["now"], http=FakeHTTP()).tick()
        tick()
        de_ids = {p.page_id for p in de}
        self.assertEqual(sum(1 for p in graph.posts.values() if p["page"] in de_ids), len(de_ids))
        n_wp = len(wp.posts)
        for p in de:                                   # страницы DE теперь получают нидерландский текст
            cfg.languages["DE"]["pages"].remove(p)
            p.lang = "NL"
            cfg.languages["NL"]["pages"].append(p)
        key = "2030-05-01 09:00"
        res = drop_lang(group_view(cfg, "NL"), "NL", key, "DE", services=services, tokens=tokens)
        self.assertIn("удалён", res)
        self.assertEqual(sum(1 for p in graph.posts.values() if p["page"] in de_ids), 0)
        self.assertEqual(len(wp.posts), n_wp - 1)
        rework_slot(cfg, "NL", key, force=True)
        for hh, mm in ((6, 20), (9, 7), (9, 37)):
            clock["now"] = clock["now"].replace(hour=hh, minute=mm)
            tick()
        per_page = {}
        for p in graph.posts.values():
            per_page[p["page"]] = per_page.get(p["page"], 0) + 1
        self.assertTrue(all(n == 1 for n in per_page.values()), per_page)   # ни одного дубля
        self.assertTrue(de_ids <= set(per_page))                            # бывшие DE-страницы получили пост
        self.assertEqual(State(st_path).slots[key]["status"], "done")


class PublishNowTests(unittest.TestCase):
    """«Опубликуй сейчас»: подготовленный выпуск выходит немедленно, его время освобождается для других ссылок."""

    def test_publish_now_moves_release_and_frees_slot(self):
        from autopost.cli import publish_now
        from autopost.config import group_view
        cfg, tmp = temp_cfg()
        cfg.settings["schedule_enabled"] = True
        tz = ZoneInfo(cfg.tz)
        clock = {"now": datetime(2030, 5, 1, 7, 1, tzinfo=tz)}
        graph, wp = FakeGraph(cfg, lambda: clock["now"]), FakeWP()
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        _queue_story(graph, cfg, tmp, "NL", 931, "Story A.")
        st_path = tmp / "state/groups/NL.json"

        def tick():
            gcfg = group_view(cfg, "NL")
            Pipeline(gcfg, State(st_path), graph, wp, Generator(gcfg, backend="mock"), tokens,
                     now_fn=lambda: clock["now"], http=FakeHTTP()).tick()
        tick()                                                        # готовит 09:00
        pages = {p.page_id for p in cfg.pages if p.group == "NL"}
        self.assertEqual(len(graph.posts), len(pages))
        self.assertTrue(all(not p["published"] for p in graph.posts.values()))
        clock["now"] = clock["now"].replace(minute=3)
        res = publish_now(cfg, "NL", "2030-05-01 09:00", services=(graph, wp, FakeHTTP()), tokens=tokens,
                          now=clock["now"])
        self.assertIn("2030-05-01 07:03", res)
        self.assertEqual(graph.posts, {})                             # отложенные на 09:00 удалены
        self.assertTrue(all(p["status"] == "publish" for p in wp.posts.values()))
        st = State(st_path)
        self.assertNotIn("2030-05-01 09:00", st.slots)
        _queue_story(graph, cfg, tmp, "NL", 932, "Story B.")         # другие ссылки — на 09:00
        for mm in (4, 20, 40):
            clock["now"] = clock["now"].replace(minute=mm)
            tick()
        live = [p for p in graph.posts.values() if p["published"]]
        self.assertEqual(len(live), len(pages))                       # вышло сразу
        self.assertTrue(all(len(graph.comments_by.get(pid, [])) == 1 for pid, p in graph.posts.items()
                            if p["published"]))
        st = State(st_path)
        self.assertEqual(st.slots["2030-05-01 07:03"]["status"], "done")
        nine = st.slots["2030-05-01 09:00"]
        self.assertTrue(nine.get("prepared"))
        self.assertNotEqual(nine["source_key"], st.slots["2030-05-01 07:03"]["source_key"])
        sched = [p for p in graph.posts.values() if not p["published"]]
        self.assertEqual(len(sched), len(pages))                      # новый выпуск 09:00 — в отложенных
        moved = st.slots["2030-05-01 07:03"]
        for lg, L in nine["langs"].items():                           # у нового выпуска — свои статьи и фото
            if L.get("generated"):
                self.assertNotEqual(L["wp_id"], moved["langs"][lg]["wp_id"])
        self.assertNotEqual(nine["source"]["wp_media_id"], moved["source"]["wp_media_id"])


class StartNowTests(unittest.TestCase):
    """«Сейчас»: внеочередной выпуск на текущую минуту, без отложенных постов, расписание не трогается."""

    def test_start_now_publishes_immediately(self):
        from autopost.cli import start_now
        from autopost.config import group_view
        cfg, tmp = temp_cfg()
        cfg.settings["schedule_enabled"] = True
        clock = {"now": datetime(2030, 5, 1, 7, 1, tzinfo=ZoneInfo(cfg.tz))}
        graph, wp = FakeGraph(cfg, lambda: clock["now"]), FakeWP()
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        st_path = tmp / "state/groups/FR.json"

        def tick():
            gcfg = group_view(cfg, "FR")
            Pipeline(gcfg, State(st_path), graph, wp, Generator(gcfg, backend="mock"), tokens,
                     now_fn=lambda: clock["now"], http=FakeHTTP()).tick()
        tick()                                                        # расписание включено, очередь пуста
        _queue_story(graph, cfg, tmp, "FR", 951, "Story S.")
        clock["now"] = clock["now"].replace(minute=3)
        start_now(cfg, "FR", now=clock["now"])
        for mm in (4, 20, 40):
            clock["now"] = clock["now"].replace(minute=mm)
            tick()
        st = State(st_path)
        self.assertEqual(st.slots["2030-05-01 07:03"]["status"], "done")
        self.assertTrue(graph.posts and all(p["published"] for p in graph.posts.values()))   # ни одного отложенного
        self.assertTrue(all(len(graph.comments_by.get(pid, [])) == 1 for pid in graph.posts))
        self.assertFalse(st.slots.get("2030-05-01 09:00", {}).get("prepared"))


class AdhocYieldTests(unittest.TestCase):
    """Прошедший слот без истории не забирает ссылки, присланные к внеочередному выпуску позже."""

    def test_overdue_slot_leaves_links_for_adhoc(self):
        from autopost.cli import start_now
        from autopost.config import group_view
        cfg, tmp = temp_cfg()
        cfg.settings["schedule_enabled"] = True
        tz = ZoneInfo(cfg.tz)
        clock = {"now": datetime(2030, 5, 1, 20, 50, tzinfo=tz)}
        graph, wp = FakeGraph(cfg, lambda: clock["now"]), FakeWP()
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        st_path = tmp / "state/groups/FR.json"

        def tick():
            gcfg = group_view(cfg, "FR")
            Pipeline(gcfg, State(st_path), graph, wp, Generator(gcfg, backend="mock"), tokens,
                     now_fn=lambda: clock["now"], http=FakeHTTP()).tick()
        tick()                                                   # очередь пуста: 21:00 без истории
        clock["now"] = datetime(2030, 5, 1, 21, 11, tzinfo=tz)
        start_now(cfg, "FR", at="2030-05-01 21:30")
        _queue_story(graph, cfg, tmp, "FR", 981, "Story Y.")
        tick()
        st = State(st_path)
        self.assertFalse(st.slots["2030-05-01 21:00"].get("source_key"))
        self.assertTrue(st.slots["2030-05-01 21:30"].get("prepared"))
        self.assertTrue(graph.posts and not any(p["published"] for p in graph.posts.values()))   # отложены на 21:30


class RetryLangTests(unittest.TestCase):
    """Пропущенный язык выпуска готовится заново; посты других языков не повторяются."""

    def test_retry_lang_publishes_only_that_language(self):
        from autopost.cli import retry_lang
        from autopost.config import group_view
        cfg, tmp = temp_cfg()
        cfg.settings["schedule_enabled"] = True
        tz = ZoneInfo(cfg.tz)
        clock = {"now": datetime(2030, 5, 1, 6, 7, tzinfo=tz)}
        graph, wp = FakeGraph(cfg, lambda: clock["now"]), FakeWP()
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        _queue_story(graph, cfg, tmp, "IT", 991, "Story L.")
        st_path = tmp / "state/groups/IT.json"

        def tick():
            gcfg = group_view(cfg, "IT")
            Pipeline(gcfg, State(st_path), graph, wp, Generator(gcfg, backend="mock"), tokens,
                     now_fn=lambda: clock["now"], http=FakeHTTP()).tick()
        tick()
        key = "2030-05-01 09:00"
        st = State(st_path)
        L = st.slots[key]["langs"]["PL"]
        pl_pages = set(L["pages"])
        for pid in pl_pages:                                     # будто текст PL так и не получился
            graph.posts = {k: v for k, v in graph.posts.items() if v["page"] != pid}
        L.update(generated=False, gen_error="заголовок пустой", gen_attempts=3)
        L.pop("wp_id", None)
        L["pages"] = {pid: {"status": "failed_final", "attempts": 0, "error": "x"} for pid in pl_pages}
        st.save()
        (tmp / "content/2030-05-01/IT/0900-PL.json").unlink(missing_ok=True)
        before = {k for k, v in graph.posts.items()}
        self.assertIn("заново", retry_lang(cfg, "IT", key, "PL"))
        clock["now"] = datetime(2030, 5, 1, 9, 20, tzinfo=tz)
        tick()
        tick()
        st = State(st_path)
        new = [v for k, v in graph.posts.items() if k not in before]
        self.assertEqual({v["page"] for v in new}, pl_pages)
        self.assertTrue(all(v["published"] for v in new))
        self.assertTrue(all(e["status"] == "done" for e in st.slots[key]["langs"]["PL"]["pages"].values()))
        self.assertIn("уже есть посты", retry_lang(cfg, "IT", key, "IT"))


class RepostNowTests(unittest.TestCase):
    """Невышедший выпуск повторяется сейчас; языки, добавленные в группу позже, получают тексты и посты."""

    def test_repost_now_adds_new_group_languages(self):
        from autopost.cli import repost_now
        from autopost.config import group_view
        cfg, tmp = temp_cfg()
        cfg.settings["schedule_enabled"] = True
        tz = ZoneInfo(cfg.tz)
        clock = {"now": datetime(2030, 5, 1, 6, 7, tzinfo=tz)}
        graph, wp = FakeGraph(cfg, lambda: clock["now"]), FakeWP()
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        extra = [p for p in cfg.pages if p.group == "IT" and p.lang != "IT"]
        self.assertTrue(extra)
        for p in extra:
            p.group = "X"                        # до перегруппировки: в группе IT только итальянские
        _queue_story(graph, cfg, tmp, "IT", 961, "Story P.")
        st_path = tmp / "state/groups/IT.json"

        def tick():
            gcfg = group_view(cfg, "IT")
            Pipeline(gcfg, State(st_path), graph, wp, Generator(gcfg, backend="mock"), tokens,
                     now_fn=lambda: clock["now"], http=FakeHTTP()).tick()
        tick()
        first = {k for k in graph.posts}
        for k in first:                         # будто Facebook их так и не опубликовал
            graph.posts.pop(k)
        for p in extra:
            p.group = "IT"
        clock["now"] = datetime(2030, 5, 1, 13, 10, tzinfo=tz)
        res = repost_now(cfg, "IT", "2030-05-01 09:00", now=clock["now"])
        self.assertIn("2030-05-01 13:10", res)
        for mm in (11, 30):
            clock["now"] = clock["now"].replace(minute=mm)
            tick()
        live = [p for p in graph.posts.values() if p["published"]]
        self.assertEqual({p["page"] for p in live}, {p.page_id for p in cfg.pages if p.group == "IT"})
        self.assertTrue(all(len(graph.comments_by.get(pid, [])) == 1 for pid in graph.posts))
        self.assertEqual(State(st_path).slots["2030-05-01 13:10"]["status"], "done")


class MoveSlotTests(unittest.TestCase):
    """Перенос подготовленного выпуска на другое время: отложенные посты получают новое время, без удаления."""

    def test_move_slot_reschedules_posts(self):
        from unittest import mock
        from autopost.cli import move_slot, start_now
        from autopost.config import group_view
        from autopost.pipeline import Pacer
        cfg, tmp = temp_cfg()
        cfg.settings["schedule_enabled"] = True
        tz = ZoneInfo(cfg.tz)
        clock = {"now": datetime(2030, 5, 1, 13, 0, tzinfo=tz)}
        graph, wp = FakeGraph(cfg, lambda: clock["now"]), FakeWP()
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        st_path = tmp / "state/groups/FR.json"

        def tick():
            gcfg = group_view(cfg, "FR")
            Pipeline(gcfg, State(st_path), graph, wp, Generator(gcfg, backend="mock"), tokens,
                     now_fn=lambda: clock["now"], http=FakeHTTP()).tick()
        tick()
        st = State(st_path)                                       # 15:00 пропущен, как в жизни
        st.slots["2030-05-01 15:00"] = {"status": "cancelled", "langs": {},
                                        "slot_iso": datetime(2030, 5, 1, 15, 0, tzinfo=tz).isoformat()}
        st.save()
        start_now(cfg, "FR", at="2030-05-01 16:00")
        _queue_story(graph, cfg, tmp, "FR", 971, "Story M.")
        tick()                                                    # 16:00 готов, посты отложены на 16:00
        st = State(st_path)
        self.assertTrue(st.slots["2030-05-01 16:00"]["prepared"])
        n = len(graph.posts)
        self.assertTrue(n > 0)
        with mock.patch("autopost.cli.datetime") as dtm:
            dtm.now.return_value = clock["now"]
            dtm.strptime = datetime.strptime
            dtm.fromisoformat = datetime.fromisoformat
            res = move_slot(cfg, "FR", "2030-05-01 16:00", "2030-05-01 18:00",
                            services=(graph, wp, FakeHTTP()), tokens=tokens, pace=Pacer(0))
        self.assertIn("перенесено отложенных постов", res)
        self.assertEqual(len(graph.posts), n)                     # ничего не удалено и не создано заново
        ts18 = int(datetime(2030, 5, 1, 18, 0, tzinfo=tz).timestamp())
        self.assertTrue(all(p["scheduled_ts"] == ts18 for p in graph.posts.values()))
        st = State(st_path)
        self.assertNotIn("2030-05-01 16:00", st.slots)
        self.assertFalse(st.slots["2030-05-01 18:00"].get("adhoc"))
        for hh, mm in ((16, 5), (18, 7), (18, 37)):
            clock["now"] = clock["now"].replace(hour=hh, minute=mm)
            tick()
        self.assertEqual(len(graph.posts), n)
        self.assertTrue(all(p["published"] for p in graph.posts.values()))
        self.assertTrue(all(len(graph.comments_by.get(pid, [])) == 1 for pid in graph.posts))
        self.assertEqual(State(st_path).slots["2030-05-01 18:00"]["status"], "done")


class RateLimitTests(unittest.TestCase):
    """Facebook ответил 368 «ограничиваем частоту»: остальные страницы не дёргаем, ждём паузу, потом публикуем."""

    def test_rate_limit_pauses_and_retries(self):
        from autopost.config import group_view
        cfg, tmp = temp_cfg()
        cfg.settings["schedule_enabled"] = True
        clock = {"now": datetime(2030, 5, 1, 6, 7, tzinfo=ZoneInfo(cfg.tz))}
        graph, wp = FakeGraph(cfg, lambda: clock["now"]), FakeWP()
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        _queue_story(graph, cfg, tmp, "IT", 941, "Story L.")
        st_path = tmp / "state/groups/IT.json"
        pages = [p for p in cfg.pages if p.group == "IT"]
        calls = []
        orig = graph.create_photo_post

        def counting(*a, **k):
            calls.append(a[0])
            return orig(*a, **k)
        graph.create_photo_post = counting
        graph.fail_next.append(("create_photo_post", None,
                                FacebookError("Мы ограничиваем частоту", code=368, subcode=1390008)))

        def tick():
            gcfg = group_view(cfg, "IT")
            Pipeline(gcfg, State(st_path), graph, wp, Generator(gcfg, backend="mock"), tokens,
                     now_fn=lambda: clock["now"], http=FakeHTTP()).tick()
        tick()                                   # 06:07 — готовим 09:00; первый же пост — 368
        self.assertEqual(len(calls), 1)          # остальные страницы не трогали
        self.assertEqual(graph.posts, {})
        clock["now"] = clock["now"].replace(minute=30)
        tick()                                   # пауза 60 минут — не публикуем
        self.assertEqual(len(calls), 1)
        clock["now"] = clock["now"].replace(hour=7, minute=10)
        tick()                                   # пауза прошла — ставим все
        self.assertEqual(len(graph.posts), len(pages))
        for hh, mm in ((9, 7), (9, 37)):
            clock["now"] = clock["now"].replace(hour=hh, minute=mm)
            tick()
        self.assertTrue(all(len(graph.comments_by.get(pid, [])) == 1 for pid in graph.posts))
        st = State(st_path)
        self.assertTrue(all(e["attempts"] <= 1 for L in st.slots["2030-05-01 09:00"]["langs"].values()
                            for e in L["pages"].values()))
        self.assertNotIn("fb_block_minutes", st.data)
        self.assertFalse(st.data["fb_blocks"]["main"].get("minutes"))

    def test_rate_limit_stops_only_that_user(self):
        """Отказ 368 у старого autopost: его страницы на паузе (во всех группах, через общий файл),
        страницы autopost2 публикуются как обычно."""
        from autopost.config import group_view
        from autopost.pipeline import Pacer
        cfg, tmp = temp_cfg()
        cfg.settings["schedule_enabled"] = True
        clock = {"now": datetime(2030, 5, 1, 6, 7, tzinfo=ZoneInfo(cfg.tz))}
        graph, wp = FakeGraph(cfg, lambda: clock["now"]), FakeWP()
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        _queue_story(graph, cfg, tmp, "IT", 942, "Story M.")
        st_path = tmp / "state/groups/IT.json"
        pages = [p.page_id for p in cfg.pages if p.group == "IT"]
        old = set(pages[:2])
        owners = {pid: ("autopost" if pid in old else "autopost2") for pid in pages}
        orig = graph.create_photo_post
        calls = []

        def picky(page_id, *a, **k):
            calls.append(page_id)
            if page_id in old:
                raise FacebookError("Мы ограничиваем частоту", code=368, subcode=1390008)
            return orig(page_id, *a, **k)
        graph.create_photo_post = picky
        guard = tmp / "state/fb_guard.json"

        def tick():
            gcfg = group_view(cfg, "IT")
            Pipeline(gcfg, State(st_path), graph, wp, Generator(gcfg, backend="mock"), tokens,
                     now_fn=lambda: clock["now"], http=FakeHTTP(),
                     pacer=Pacer(0, owners=owners, path=guard)).tick()
        tick()
        self.assertEqual({k.split('_')[0] for k in graph.posts}, set(pages) - old)   # autopost2 — все свои страницы
        self.assertEqual(sum(1 for c in calls if c in old), 1)          # старый — одна попытка и стоп
        self.assertEqual(json.loads(guard.read_text())["autopost"]["minutes"], 60)
        clock["now"] = clock["now"].replace(minute=30)
        tick()                                                          # пауза — старого не трогаем
        self.assertEqual(sum(1 for c in calls if c in old), 1)
        graph.create_photo_post = orig
        clock["now"] = clock["now"].replace(hour=7, minute=10)
        tick()                                                          # пауза прошла — ставим и его страницы
        self.assertEqual({k.split('_')[0] for k in graph.posts}, set(pages))
        self.assertFalse(json.loads(guard.read_text())["autopost"].get("minutes"))


    def test_one_refusal_per_run_and_slow_user_pace(self):
        import time as _t
        from autopost.pipeline import Pacer
        now = datetime(2030, 5, 1, 0, 0, tzinfo=ZoneInfo("Asia/Yerevan"))
        p = Pacer(0, owners={"a": "autopost"}, per_owner={"autopost": 0.2})
        self.assertEqual(p.block("autopost", now), 60)
        self.assertEqual(p.block("autopost", now), 60)       # вторая группа того же запуска — не удваиваем
        p2 = Pacer(0, per_owner={"autopost": 0.2})
        p2.blocks = p.blocks
        self.assertEqual(p2.block("autopost", now + timedelta(minutes=61)), 120)   # новый запуск — удваиваем
        t0 = _t.monotonic()
        p.wait("autopost"); p.wait("autopost"); p.wait("autopost2")
        self.assertGreaterEqual(_t.monotonic() - t0, 0.2)     # медленный — не чаще раза в 0,2 с; другой не ждёт

    def test_no_late_post_after_next_release_started(self):
        from autopost.config import group_view
        cfg, tmp = temp_cfg()
        tz = ZoneInfo(cfg.tz)
        st = State(tmp / "state/groups/IT.json")
        st.data.setdefault("slots", {}).update({
            "2030-05-01 21:00": {"status": "prepared", "source": {"link": "a"}},
            "2030-05-02 00:00": {"status": "prepared", "source": {"link": "b"}},
            "2030-05-02 03:00": {"status": "no_source"}})
        gcfg = group_view(cfg, "IT")
        pl = Pipeline(gcfg, st, FakeGraph(cfg, lambda: None), FakeWP(), Generator(gcfg, backend="mock"), {},
                      now_fn=lambda: datetime(2030, 5, 2, 0, 7, tzinfo=tz), http=FakeHTTP())
        d21 = datetime(2030, 5, 1, 21, 0, tzinfo=tz)
        self.assertTrue(pl._newer_release_started(d21, datetime(2030, 5, 2, 0, 7, tzinfo=tz)))
        self.assertFalse(pl._newer_release_started(d21, datetime(2030, 5, 1, 22, 10, tzinfo=tz)))
        self.assertFalse(pl._newer_release_started(datetime(2030, 5, 2, 0, 0, tzinfo=tz),
                                                   datetime(2030, 5, 2, 3, 30, tzinfo=tz)))   # без истории не в счёт


class PfbidLinkTests(unittest.TestCase):
    def test_pfbid_story_link_resolved_via_posts_redirect(self):
        from unittest import mock
        from autopost import sources
        from autopost.sources import normalize_link
        a = "https://m.facebook.com/story.php?story_fbid=pfbid0TPo9krJ&id=100044802900629"
        def fake(v):
            if "/posts/pfbid0TPo9krJ" in v:
                return {"url": "https://www.facebook.com/100044802900629/posts/slug-text/122106012902556305"}
            return {"url": v}
        cfg, tmp = temp_cfg()
        with mock.patch.object(sources, "resolve_share_link", side_effect=fake):
            self.assertEqual(normalize_link(a, State(tmp / "state/x.json")), "100044802900629_122106012902556305")

    def test_pfbid_links_resolved_via_page_posts(self):
        from unittest import mock
        from autopost import sources
        from autopost.sources import _pfbid_parts, normalize_link, parse_link
        a = "https://m.facebook.com/story.php?story_fbid=pfbid0TPo9krJ&id=100044802900629&mibextid=wwXIfr"
        b = "https://www.facebook.com/61566689175708/posts/pfbid028QjoR6/?mibextid=wwXIfr"
        self.assertEqual(_pfbid_parts(a), ("100044802900629", "pfbid0TPo9krJ"))
        self.assertEqual(_pfbid_parts(b), ("61566689175708", "pfbid028QjoR6"))

        class G:
            def get(self, path, token, **kw):
                page = path.split("/")[0]
                return {"data": [{"id": f"{page}_111", "permalink_url": f"https://www.facebook.com/{page}/posts/pfbidOTHER"},
                                 {"id": f"{page}_222", "permalink_url": f"https://www.facebook.com/{page}/posts/pfbid0TPo9krJ"},
                                 {"id": f"{page}_333", "permalink_url": f"https://www.facebook.com/{page}/posts/pfbid028QjoR6"}]}
        cfg, tmp = temp_cfg()
        st = State(tmp / "state/x.json")
        with mock.patch.object(sources, "resolve_share_link", return_value={}):
            ra = normalize_link(a, st, G(), {"p": "T"}, "")
            rb = normalize_link(b, st, G(), {"p": "T"}, "")
        self.assertEqual(ra, "100044802900629_222")
        self.assertEqual(rb, "61566689175708_333")
        self.assertEqual(parse_link(ra, {})["part"], "222")
        with mock.patch.object(sources, "resolve_share_link", side_effect=AssertionError("кэш")):
            self.assertEqual(normalize_link(a, st, G(), {"p": "T"}, ""), ra)    # запомнено в состоянии

class LateLinkTests(unittest.TestCase):
    def test_link_sent_after_release_goes_to_next_release(self):
        """Выпуск 09:00 прошёл без истории; ссылка пришла в 09:45 — она идёт в 12:00, а не «догоняет» 09:00."""
        from autopost.config import group_view
        cfg, tmp = temp_cfg()
        cfg.settings["schedule_enabled"] = True
        clock = {"now": datetime(2030, 5, 1, 6, 7, tzinfo=ZoneInfo(cfg.tz))}
        graph, wp = FakeGraph(cfg, lambda: clock["now"]), FakeWP()
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        g = "NL"
        gcfg = group_view(cfg, g)
        st = State(tmp / f"state/groups/{g}.json")
        (tmp / "queue").mkdir(exist_ok=True)
        q = tmp / "queue" / f"{g}.txt"
        q.write_text("# пусто\n", encoding="utf-8")

        def tick():
            Pipeline(gcfg, st, graph, wp, Generator(gcfg, backend="mock"), tokens,
                     now_fn=lambda: clock["now"], http=FakeHTTP()).tick()

        tick()                                            # 06:07 — очередь пуста
        pid = cfg.pages[0].page_id
        graph.source_posts.setdefault(pid, []).append({
            "id": f"{pid}_77", "message": STORY, "full_picture": "https://x/y.jpg",
            "attachments": {"data": [{"media_type": "photo"}]}})
        clock["now"] = clock["now"].replace(hour=9, minute=45)
        q.write_text(f"{pid}_77\n", encoding="utf-8")    # ссылка пришла через 45 минут после 09:00
        tick()
        self.assertEqual(st.slots["2030-05-01 09:00"]["status"], "missed")
        self.assertEqual(st.slots["2030-05-01 12:00"]["source_key"], f"post:{pid}_77")
        self.assertTrue(all(p["scheduled_ts"] for p in graph.posts.values()))  # встали в отложенные на 12:00

    def test_share_link_is_resolved_and_cached(self):
        from unittest import mock
        from autopost import sources
        st = State(Path(tempfile.mkdtemp()) / "s.json")
        with mock.patch.object(sources, "resolve_share_link",
                               return_value={"story_fbid": "1698682471650445", "id": "100045261260562"}) as m:
            url = sources.normalize_link("https://www.facebook.com/share/p/1CCVbeknM2/", st)
            url2 = sources.normalize_link("https://www.facebook.com/share/p/1CCVbeknM2/", st)
        self.assertEqual(url, "https://www.facebook.com/story.php?story_fbid=1698682471650445&id=100045261260562")
        self.assertEqual(url, url2)
        self.assertEqual(m.call_count, 1)           # второй раз — из сохранённого состояния
        self.assertEqual(sources.normalize_link("https://www.facebook.com/x/posts/1", st),
                         "https://www.facebook.com/x/posts/1")


class RepostTests(unittest.TestCase):
    def test_repost_uses_same_texts_and_article(self):
        """Повтор выпуска: новые посты в ленте в новый час, ссылка в комментарии — на ту же статью, новых статей нет."""
        from autopost.cli import repost_slot
        from autopost.config import group_view
        cfg, tmp = temp_cfg()
        cfg.settings["schedule_enabled"] = True
        clock = {"now": datetime(2030, 5, 1, 6, 7, tzinfo=ZoneInfo(cfg.tz))}
        graph, wp = FakeGraph(cfg, lambda: clock["now"]), FakeWP()
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        g = "FR"   # группа одного языка: одна статья на выпуск
        gcfg = group_view(cfg, g)
        st = State(tmp / f"state/groups/{g}.json")
        (tmp / "queue").mkdir(exist_ok=True)
        pid = cfg.pages[0].page_id
        graph.source_posts.setdefault(pid, []).append({
            "id": f"{pid}_5", "message": STORY, "full_picture": "https://x/y.jpg",
            "attachments": {"data": [{"media_type": "photo"}]}})
        (tmp / "queue" / f"{g}.txt").write_text(f"{pid}_5\n", encoding="utf-8")

        def tick():
            Pipeline(gcfg, st, graph, wp, Generator(gcfg, backend="mock"), tokens,
                     now_fn=lambda: clock["now"], http=FakeHTTP()).tick()

        for hh, mm in ((6, 7), (9, 7)):
            clock["now"] = clock["now"].replace(hour=hh, minute=mm)
            tick()
        first = dict(graph.posts)
        self.assertEqual(st.slots["2030-05-01 09:00"]["status"], "done")
        self.assertEqual(repost_slot(cfg, st, g, "2030-05-01 09:00", "2030-05-01 15:00"), "поставлен")
        for hh, mm in ((9, 37), (15, 7)):
            clock["now"] = clock["now"].replace(hour=hh, minute=mm)
            tick()
        new_posts = {k: v for k, v in graph.posts.items() if k not in first}
        self.assertEqual(len(new_posts), len(first))                      # те же страницы — ещё раз
        self.assertTrue(all(v["scheduled_ts"] == datetime(2030, 5, 1, 15, 0, tzinfo=ZoneInfo(cfg.tz)).timestamp()
                            for v in new_posts.values()))
        self.assertEqual(len(wp.posts), 1)                                # статья не создавалась заново
        link = graph.comments_by[next(iter(first))][0]["message"]
        self.assertTrue(all(graph.comments_by[k][0]["message"] == link for k in new_posts))
        self.assertEqual(st.slots["2030-05-01 15:00"]["status"], "done")
