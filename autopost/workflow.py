"""Расписание запусков GitHub Actions строится из часов публикаций (settings.yaml → slots).

Для каждого слота S: S+0:07, S+0:22, S+0:37, S+0:52 (первые комментарии; заодно подготовка следующих
выпусков на 24 часа вперёд); после последнего слота дня ещё S+1:07 и S+2:07 (догоняющие).
Минуты сдвинуты от :00, когда GitHub перегружен. Cron в UTC — пересчитываем из часового пояса
(в Asia/Yerevan нет перехода на летнее время, поэтому пересчёт постоянный).
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# GitHub выполняет расписание с большими пропусками, поэтому: подготовка идёт на 24 часа вперёд
# (посты и статьи встают в отложенные и выходят сами точно в час), а после каждого выпуска —
# 4 попытки за час, чтобы быстро добавить первые комментарии.
OFFSETS_MIN = (7, 22, 37, 52)
LAST_SLOT_EXTRA_MIN = (67, 127)
START, END = "# >>> cron (генерируется: python -m autopost render-workflow)", "# <<< cron"
WORKFLOW = ".github/workflows/autopost.yml"


def last_slot_of_day(slots) -> str:
    """Слот, после которого самый длинный перерыв (например, 00:00 при часах 12–24)."""
    mins = sorted(int(x[:2]) * 60 + int(x[3:]) for x in slots)
    gaps = [((mins[(i + 1) % len(mins)] - m) % 1440 or 1440, m) for i, m in enumerate(mins)]
    m = max(gaps)[1]
    return f"{m // 60:02d}:{m % 60:02d}"


def tick_times_utc(cfg) -> list:
    tz = ZoneInfo(cfg.tz)
    ref = datetime(2026, 1, 15, tzinfo=tz)
    out = set()
    for s in cfg.slots:
        h, m = map(int, s.split(":"))
        base = ref.replace(hour=h, minute=m)
        offs = OFFSETS_MIN + (LAST_SLOT_EXTRA_MIN if s == last_slot_of_day(cfg.slots) else ())
        for off in offs:
            t = (base + timedelta(minutes=off)).astimezone(ZoneInfo("UTC"))
            out.add((t.hour, t.minute))
    return sorted(out)


def render_cron_block(cfg, indent="    ") -> str:
    by_min = defaultdict(list)
    for h, m in tick_times_utc(cfg):
        by_min[m].append(h)
    lines = [f"{indent}{START}",
             f"{indent}# Часы публикаций ({cfg.tz}): {', '.join(cfg.slots)}"]
    for m in sorted(by_min):
        hours = ",".join(str(h) for h in sorted(set(by_min[m])))
        lines.append(f'{indent}- cron: "{m} {hours} * * *"')
    lines.append(f"{indent}{END}")
    return "\n".join(lines)


def write_workflow(cfg) -> str:
    p = cfg.path(WORKFLOW)
    text = p.read_text(encoding="utf-8")
    pat = re.compile(rf"^[ \t]*{re.escape(START)}.*?{re.escape(END)}[ \t]*$", re.S | re.M)
    if not pat.search(text):
        raise RuntimeError(f"в {WORKFLOW} нет блока расписания")
    new = pat.sub(lambda _m: render_cron_block(cfg), text)
    p.write_text(new, encoding="utf-8")
    return str(p)


def workflow_in_sync(cfg) -> bool:
    text = cfg.path(WORKFLOW).read_text(encoding="utf-8")
    return render_cron_block(cfg) in text
