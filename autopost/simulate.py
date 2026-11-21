"""Сухой прогон: N дней расписания на поддельных Facebook/WordPress и тестовых текстах.

Проверяет: 5 слотов в день × все страницы, отсутствие дублей, первый комментарий у каждого поста,
повтор только неудавшихся шагов (внедряются сбои), повторный запуск ничего не задваивает.
"""
from __future__ import annotations

import shutil
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config as config_mod
from .config import LANG_ORDER
from .fakes import FakeGraph, FakeHTTP, FakeWP
from .fb import FacebookError
from .generate import Generator
from .pipeline import Pipeline
from .state import State
from .workflow import LAST_SLOT_EXTRA_MIN, OFFSETS_MIN, last_slot_of_day
from .wp import WordPressError

STORY = ("When Anna found an old letter hidden behind the kitchen cupboard, she did not expect it to change "
         "everything she believed about her family. The letter was written by her grandmother many years ago, "
         "and it told a story that nobody had ever dared to tell. ") * 4


def run_simulation(cfg, days=2, drop=0.0, seed=7):
    """Сухой прогон по группам: у каждой группы своя история в каждом выпуске (истории — поддельные посты)."""
    tmp = Path(tempfile.mkdtemp(prefix="autopost-sim-"))
    for d in ("config", "prompts"):
        shutil.copytree(cfg.path(d), tmp / d)
    (tmp / "state").mkdir()
    sim_cfg = config_mod.load(tmp)
    sim_cfg.settings["schedule_enabled"] = True
    tz = ZoneInfo(sim_cfg.tz)
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    clock = {"now": start}
    graph = FakeGraph(sim_cfg, lambda: clock["now"])
    wp = FakeWP()
    tokens = {p.page_id: f"PT-{p.page_id}" for p in sim_cfg.pages}

    groups = {}
    for gi, g in enumerate(sim_cfg.groups()):
        gcfg = config_mod.group_view(sim_cfg, g)
        src = gcfg.settings["sources"]
        src["order"] = ["auto"]                       # без сети: истории — «посты» на страницах группы
        src["auto"].update(enabled=True, pages="all", lookback_hours=24 * (days + 2))
        st = State(tmp / f"state/groups/{g}.json")
        st.data["source_cutoff"] = (start - timedelta(days=1)).isoformat()
        for i in range(5 * days + 3):
            page = gcfg.pages[(i * 7) % len(gcfg.pages)]
            ts = (start - timedelta(hours=10) + timedelta(minutes=13 * i + gi)).timestamp()
            graph.source_posts.setdefault(page.page_id, []).append({
                "id": f"{page.page_id}_{700000 + 100 * gi + i}", "message": f"Story {g} #{i}. " + STORY,
                "full_picture": f"https://scontent.example/{g}-{i}.jpg", "created_time": f"{int(ts)}",
                "attachments": {"data": [{"media_type": "photo"}]}, "_ts": int(ts)})
        groups[g] = (gcfg, st, Generator(gcfg, backend="mock"))

    # внедряем сбои
    some = [p.page_id for p in sim_cfg.pages]
    graph.fail_next += [
        ("create_photo_post", some[3], FacebookError("Service temporarily unavailable", code=2)),
        ("comment", some[10], FacebookError("Please retry", code=1)),
    ]
    lost = FacebookError("сеть: connection reset")
    lost.after_create = True
    graph.fail_next.append(("create_photo_post", some[20], lost))
    wp.fail_next.append(("create_post", WordPressError("Bad gateway", 502)))

    ticks = []
    for d in range(days):
        day = start + timedelta(days=d)
        for s in sim_cfg.slots:
            h, m = map(int, s.split(":"))
            base = day.replace(hour=h, minute=m)
            offs = OFFSETS_MIN + (LAST_SLOT_EXTRA_MIN if s == last_slot_of_day(sim_cfg.slots) else ())
            ticks += [base + timedelta(minutes=o) for o in offs]
    ticks = sorted(set(ticks))
    if drop:  # GitHub пропускает часть запусков по расписанию — имитируем
        import random
        rnd = random.Random(seed)
        ticks = [t for t in ticks if rnd.random() >= drop] or ticks[:1]
    log = []

    def tick_all():
        for gcfg, st, gen in groups.values():
            r = Pipeline(gcfg, st, graph, wp, gen, tokens, now_fn=lambda: clock["now"], http=FakeHTTP()).tick()
            log.append((clock["now"], r))

    for t in ticks:
        clock["now"] = t
        for _rep in range(2):  # каждый запуск дважды — проверка идемпотентности
            tick_all()
    clock["now"] = ticks[-1] + timedelta(hours=4)  # хвост: пусть пройдёт время после последнего слота
    tick_all()

    # ---- проверки
    problems = []
    end = start + timedelta(days=days)  # выпуски, подготовленные заранее за пределами периода, не проверяем
    end_ts = end.timestamp()
    total_expected, slots_total, sources_all = 0, 0, []
    for g, (gcfg, st, _gen) in groups.items():
        slots = {k: s for k, s in st.slots.items() if datetime.fromisoformat(s["slot_iso"]) < end}
        slots_total += len(slots)
        bad = {k: s.get("status") for k, s in slots.items() if s.get("status") != "done"}
        # выпуски, час которых наступил уже после первого запуска (раньше включения — не публикуются)
        need = sum(1 for d in range(days) for x in sim_cfg.slots
                   if (start + timedelta(days=d)).replace(hour=int(x[:2]), minute=int(x[3:])) >= ticks[0])
        if bad or len(slots) < need:
            problems.append(f"{g}: слоты не завершены: {bad or len(slots)}")
        for s in slots.values():
            sources_all.append(s.get("source_key"))
            if s.get("source"):
                total_expected += sum(1 for p in gcfg.pages if p.page_id != s["source"]["page_id"])
    in_period = {pid: p for pid, p in graph.posts.items() if (p["scheduled_ts"] or p["created_ts"]) < end_ts}
    per_page_ts = Counter((p["page"], p["scheduled_ts"] or p["message"][:30]) for p in graph.posts.values())
    dups = [k for k, c in per_page_ts.items() if c > 1]
    if dups:
        problems.append(f"дубли постов: {dups[:3]}")
    no_comment = [pid for pid in in_period if len(graph.comments_by.get(pid, [])) != 1]
    if no_comment:
        problems.append(f"постов без ровно одного комментария: {len(no_comment)}")
    if len(sources_all) != len(set(sources_all)):
        problems.append("одна история использована дважды")
    if len(in_period) != total_expected:
        problems.append(f"постов {len(in_period)}, ожидалось {total_expected}")
    wp_pub = [p for p in wp.posts.values() if p["status"] == "publish"]
    if len(wp_pub) < slots_total:
        problems.append(f"статей опубликовано {len(wp_pub)}, ожидалось {slots_total}")
    if len(wp.media) < slots_total:
        problems.append(f"фото в медиатеке {len(wp.media)}, ожидалось {slots_total} (у каждой группы своё)")
    from .config import site_for
    prefixes = tuple({site_for(sim_cfg.settings, g)["comment_link_format"].split("{")[0] for g in sim_cfg.groups()})
    # ссылка — последняя строка комментария (в формате «по частям» перед ней — часть 2)
    links_ok = all(graph.comments_by[pid][0]["message"].strip().splitlines()[-1].startswith(prefixes)
                   for pid in graph.posts if graph.comments_by.get(pid))
    if not links_ok:
        problems.append("ссылка в комментарии не того формата")
    on_time = sum(1 for p in in_period.values() if p["scheduled_ts"])
    retried = sum(1 for _t, r in log for e in r.errors)
    finals = [e for _t, r in log for e in r.final_errors]
    if finals:
        problems.append(f"окончательные ошибки: {finals[:3]}")

    md = ["## Сухой прогон по группам (без сети, тестовые тексты)", "",
          f"Период: {days} дн. × {len(sim_cfg.slots)} выпусков ({', '.join(sim_cfg.slots)}, {sim_cfg.tz}); "
          f"групп: {len(groups)} ({', '.join(sim_cfg.group_label(g) for g in groups)}); "
          f"запусков: {len(ticks)} (каждый выполнен дважды для проверки дублей).", "",
          f"- Историй: **{slots_total}** (у каждой группы своя в каждом выпуске)",
          f"- Постов в Facebook: **{len(in_period)}** (из них ушли в отложенные точно ко времени: {on_time})",
          f"- Первых комментариев: **{sum(len(graph.comments_by.get(pid, [])) for pid in in_period)}**",
          f"- Статей WordPress: **{len(wp.posts)}** (опубликовано: {len(wp_pub)}), фото в медиатеке: {len(wp.media)}",
          f"- Внедрено сбоев: 4 (FB-пост, комментарий, обрыв связи после создания поста, ошибка WordPress) — "
          f"временных ошибок в журналах: {retried}, все повторены автоматически",
          "- Повторные запуски не создали ни одного дубля" if not dups else "- ❌ найдены дубли", ""]
    md += ["### Итог: ✅ всё сходится" if not problems else "### Итог: ❌ проблемы", *[f"- {p}" for p in problems]]
    shutil.rmtree(tmp, ignore_errors=True)
    return not problems, "\n".join(md) + "\n"
