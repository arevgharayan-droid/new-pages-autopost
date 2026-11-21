"""Тест стоимости и качества: одни и те же истории переписываются разными моделями Claude.

Ничего не публикует и не меняет state.json: читает истории (очередь + недавние посты страниц),
генерирует тексты, считает токены и цену по тарифам Claude API, сохраняет тексты и отчёт.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .fb import FacebookError, Graph
from .generate import GenerationError, Generator, parse_output, sanitize_article, validate, words
from .sources import SourceError, fetch_post, manual_links, parse_link
from .state import State, text_fingerprint
from .tokens import load_tokens

# Цена за 1 млн токенов, Claude API (октябрь 2026). Batch API — половина.
PRICES = {
    "sonnet": {"in": 2.00, "out": 10.00, "cache_write": 2.50, "cache_read": 0.10},
    "haiku": {"in": 0.10, "out": 0.50, "cache_write": 0.125, "cache_read": 0.01},
}
TEXTS_PER_MONTH = 250 * 30  # 100 страниц = 50 пар × 5 выпусков в день
LANG_ROTATION = ["EN", "FR", "IT", "RU", "DE", "TR", "NL"]


def _family(model: str) -> str | None:
    m = model.lower()
    for fam in PRICES:
        if fam in m:
            return fam
    return None


def usage_cost(last_usage: dict | None, model: str) -> dict:
    """Токены и цена одного обращения. Кэш считаем как обычный вход (как при прямом вызове API)."""
    res = {"in": 0, "out": 0, "cost": 0.0, "models": {}}
    if not last_usage:
        return res
    mu = last_usage.get("modelUsage") or {}
    rows = []
    if mu:
        for name, u in mu.items():
            rows.append((name, (u.get("inputTokens") or 0) + (u.get("cacheReadInputTokens") or 0)
                         + (u.get("cacheCreationInputTokens") or 0), u.get("outputTokens") or 0, u.get("costUSD")))
    else:
        u = last_usage.get("usage") or {}
        rows.append((model, (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                     + (u.get("cache_creation_input_tokens") or 0), u.get("output_tokens") or 0, None))
    for name, tin, tout, cli_cost in rows:
        fam = _family(name)
        if fam:
            cost = tin / 1e6 * PRICES[fam]["in"] + tout / 1e6 * PRICES[fam]["out"]
        else:
            cost = float(cli_cost or 0)
        res["in"] += tin
        res["out"] += tout
        res["cost"] += cost
        res["models"][name] = {"in": tin, "out": tout}
    return res


def collect_stories(cfg, graph, tokens: dict, n: int, log) -> list:
    """Истории для теста: сначала ссылки из очереди, потом недавние посты разных страниц (только чтение)."""
    st = State(cfg.path("state/state.json"))
    stories, seen = [], set()

    def add(text, page_id, origin):
        fp = text_fingerprint(text)
        if fp in seen or len(text) < 300:
            return
        seen.add(fp)
        stories.append({"text": text, "page_id": page_id, "origin": origin})

    p2p = cfg.profile_to_page()
    for link in manual_links(cfg):
        if len(stories) >= n:
            break
        try:
            info = parse_link(link, p2p)
            post = fetch_post(graph, info, tokens, cfg.fallback_source_page_id)
            add(post["text"], post.get("owner_page_id") or info["page_id"], "очередь")
        except SourceError as e:
            log(f"очередь: {link[:80]}: {e}")

    # по одной истории с разных страниц, чередуя языки
    by_lang = {lg: [p for p in cfg.pages if p.lang == lg and tokens.get(p.page_id)] for lg in LANG_ROTATION}
    since = int((datetime.now(timezone.utc) - timedelta(days=14)).timestamp())
    rnd = 0
    while len(stories) < n and any(by_lang.values()):
        for lg in LANG_ROTATION:
            if len(stories) >= n:
                break
            if not by_lang[lg]:
                continue
            page = by_lang[lg].pop(0)
            try:
                posts = graph.page_posts(page.page_id, tokens[page.page_id], since, limit=15)
            except FacebookError as e:
                log(f"{page.name}: {e.short()}")
                continue
            for post in posts:
                msg = post.get("message") or ""
                if len(msg) >= 300 and not st.is_our_output(post["id"], msg):
                    before = len(stories)
                    add(msg, page.page_id, f"страница {page.name.strip()}")
                    if len(stories) > before:
                        break
        rnd += 1
        if rnd > 20:
            break
    return stories[:n]


def _one(cfg, model: str, lang: str, source: str, backend: str) -> dict:
    gen = Generator(cfg, backend=backend)
    gen.model = model
    prompt = (gen.template.replace("{LANGUAGE}", cfg.lang_name(lang))
              .replace("{SOURCE_WORDS}", str(words(source)))
              .replace("{SOURCE}", source.strip()))
    total = {"in": 0, "out": 0, "cost": 0.0}
    attempts, errors_log, out = 0, [], None
    t0 = time.time()
    for attempt in range(1, gen.max_attempts + 1):
        attempts = attempt
        try:
            raw, used = gen._complete(prompt, lang)
        except GenerationError as e:
            errors_log.append(str(e))
            continue
        uc = usage_cost(gen.last_usage, model)
        for k in total:
            total[k] += uc[k]
        try:
            title, fb, art_raw = parse_output(raw)
            article = sanitize_article(art_raw)
            errors, warnings = validate(lang, source, title, fb, article)
        except GenerationError as e:
            errors_log.append(str(e))
            continue
        if errors:
            errors_log.append("; ".join(errors))
            continue
        out = {"title": title, "fb_text": fb.strip(), "article_html": article, "warnings": warnings,
               "backend": used, "fb_words": words(fb), "article_words": words(article)}
        break
    return {"model": model, "lang": lang, "ok": out is not None, "attempts": attempts, "errors": errors_log,
            "tokens_in": total["in"], "tokens_out": total["out"], "cost_usd": round(total["cost"], 5),
            "seconds": round(time.time() - t0, 1), "result": out}


def run(cfg, n: int = 10, models=("claude-sonnet-5-5", "claude-haiku-5-5"), parallel: int = 4) -> int:
    from .cli import summary  # избегаем циклического импорта

    notes = []
    log = notes.append
    tokens = load_tokens(cfg)
    graph = Graph(cfg.graph_version)
    stories = collect_stories(cfg, graph, tokens, n, log)
    if not stories:
        summary("## Тест стоимости\n\n❌ Не нашлось ни одной истории для теста.\n" +
                "\n".join(f"- {x}" for x in notes), "previews/cost-test.md", cfg)
        return 1

    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        backend = "subscription"
    elif os.environ.get("ANTHROPIC_API_KEY"):
        backend = "api"
    else:
        backend = "mock"
    langs = [lg for lg in LANG_ROTATION if cfg.pages_for(lg)]
    jobs = []
    for i, s in enumerate(stories):
        lang = langs[i % len(langs)]
        for m in models:
            jobs.append((i, lang, m, s["text"]))

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futs = [ex.submit(_one, cfg, m, lang, text, backend) for (_i, lang, m, text) in jobs]
        results = [f.result() for f in futs]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    folder = cfg.path(f"content/tests/cost-test-{stamp}")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "stories.json").write_text(json.dumps(stories, ensure_ascii=False, indent=1), encoding="utf-8")
    for (i, lang, m, _t), r in zip(jobs, results):
        r["story"] = i + 1
        r["source_words"] = words(stories[i]["text"])
        name = f"{i + 1:02d}-{lang}-{m.replace('claude-', '')}.json"
        (folder / name).write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")

    md = [f"## Тест стоимости и качества ({stamp} UTC)", "",
          f"Историй: **{len(stories)}**, моделей: **{len(models)}**, способ: {backend}. Ничего не публиковалось.", "",
          "| # | Язык | Слов в истории | Модель | Готово | Попыток | Токены вход/выход | Цена, $ | Секунд |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        md.append(f"| {r['story']} | {r['lang']} | {r['source_words']} | {r['model'].replace('claude-', '')} | "
                  f"{'✅' if r['ok'] else '❌'} | {r['attempts']} | {r['tokens_in']}/{r['tokens_out']} | "
                  f"{r['cost_usd']:.4f} | {r['seconds']} |")
    md += ["", "### Итог по моделям", "",
           "| Модель | Готово | Средняя цена текста, $ | 7 500 текстов в месяц, $ | То же через Batch API, $ |",
           "|---|---|---|---|---|"]
    for m in models:
        rs = [r for r in results if r["model"] == m]
        ok = sum(r["ok"] for r in rs)
        avg = sum(r["cost_usd"] for r in rs) / len(rs) if rs else 0
        md.append(f"| {m.replace('claude-', '')} | {ok}/{len(rs)} | {avg:.4f} | {avg * TEXTS_PER_MONTH:.0f} | "
                  f"{avg * TEXTS_PER_MONTH / 2:.0f} |")
    md += ["", "Цена посчитана по тарифам Claude API (Sonnet 5.5: $2/$10, Haiku 5.5: $0.10/$0.50 за 1 млн токенов), "
           "включая повторные попытки. Тексты: `" + str(folder.relative_to(cfg.root)) + "`."]
    errs = [f"- #{r['story']} {r['model']}: {e}" for r in results for e in r["errors"]]
    if errs:
        md += ["", "### Замечания проверки", *errs[:40]]
    if notes:
        md += ["", "### Источники", *[f"- {x}" for x in notes[:20]]]
    summary("\n".join(md) + "\n", "previews/cost-test.md", cfg)
    return 0 if any(r["ok"] for r in results) else 1
