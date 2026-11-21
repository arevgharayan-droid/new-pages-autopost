"""Генерация текстов Claude: заголовок, текст для Facebook и статья для сайта.

Бэкенды:
  subscription — Claude Code CLI (`claude -p`) с токеном подписки CLAUDE_CODE_OAUTH_TOKEN
  api          — Claude API с ключом ANTHROPIC_API_KEY
  mock         — тестовые тексты без обращения к Claude (для сухого прогона)
  auto         — subscription, а при сбое — api (если ключ задан)
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

import requests

ALLOWED_TAGS = {"p", "h2", "strong", "em", "ul", "li", "br"}

STOPWORDS = {
    "FR": "le la les et est une des du que qui pas pour dans avec il elle je ne sur au mais son sa ses était".split(),
    "IT": ("il lo gli e è una che non per con del della di mi ha sono ma come anche era suo sua nel alla "
           "lei lui quel quella questo questa ogni dopo aveva perché").split(),
    "EN": "the and is was to of in that it he she with for his her but not on at you they had".split(),
    "NL": "de het een en is van dat niet op met voor zijn haar maar ik je hij ze er te was".split(),
    "RU": "и в не на что он она я с как это по но его её был была они мы вы к у из за так же всё".split(),
    "TR": ("ve bir bu da de ne için ile çok ama o ben sen gibi daha kadar sonra her şey değil "
           "onun bana ona benim olarak").split(),
    "DE": "der die das und ist nicht ein eine ich sie er zu mit den von auf es war aber dass sich".split(),
    "PT": ("não os um uma do da dos das em no na nos nas ele ela eu seu foi ao pelo pela isso esta quando mais "
           "para com minha meu você tinha estava então muito").split(),
    # польский и чешский похожи — только слова, которых нет в другом языке
    "PL": ("się nie jest był była który która kiedy tylko ponieważ przez już jego mnie dla też jej "
           "jak ale tak był było wszystko bardzo jeszcze mój moja").split(),
    "CS": ("není je byl byla který která když jen protože také už pro jsem jsou mě jí jeho "
           "jako ale tak bylo všechno velmi ještě můj moje se že").split(),
    # украинский — только слова, которых нет в русском списке
    "UK": ("і що та це як від був була була вона він але їх її щоб коли дуже тому після ми ви вони його "
           "мене мені все теж вже ще тут свою своє її було".split()),
    # испанский — без слов, общих с португальским и итальянским
    "ES": ("el los las y del pero muy porque cuando fue sus había hasta también yo él ella tenía estaba "
           "después siempre nunca algo qué cómo nada".split()),
    "RO": ("și în nu cu pe este fost care că mai lui ei dar foarte pentru acum când după avea sunt "
           "această acest însă doar ea și-a".split()),
    "AR": ("في من على إلى أن هذا هذه كان كانت لم لا ما مع عن هو هي التي الذي قد كل بعد عندما ثم لكن".split()),
}


class GenerationError(Exception):
    pass


_install_lock = threading.Lock()


def ensure_claude_cli() -> str:
    """Claude Code CLI ставится только когда реально нужен (экономит минуты GitHub Actions)."""
    with _install_lock:
        exe = shutil.which("claude")
        if exe:
            return exe
        npm = shutil.which("npm")
        if not npm:
            raise GenerationError("нет npm для установки Claude Code CLI")
        proc = subprocess.run([npm, "install", "-g", "@anthropic-ai/claude-code"], capture_output=True,
                              text=True, timeout=600)
        exe = shutil.which("claude")
        if not exe:
            raise GenerationError(f"не удалось установить Claude Code CLI: {proc.stderr[-300:]}")
        return exe


@dataclass
class Generated:
    title: str
    fb_text: str
    article_html: str
    backend: str
    model: str
    warnings: list
    comment_text: str = ""     # формат «по частям»: текст первого комментария (часть 2); ссылка добавляется при публикации

    def as_dict(self) -> dict:
        return {"title": self.title, "fb_text": self.fb_text, "article_html": self.article_html,
                "comment_text": self.comment_text,
                "backend": self.backend, "model": self.model, "warnings": self.warnings}


# --- формат «по частям» (с 17.11.2026, как у страниц Georgia Daily Stories, One też czują, Er fragte sie lachte):
# пост = заголовок + часть 1 (обрыв на интриге) + «спасибо… продолжение в первом комментарии 👇»;
# первый комментарий = «ЧАСТЬ 2» + часть 2 (новый обрыв) + «напишите „Да“» + «ЧАСТЬ 3» + ссылка;
# статья на сайте = вся история: часть 1, «ЧАСТЬ 2», «ЧАСТЬ 3» с развязкой.
PART_WORD = {"FR": "PARTIE", "IT": "PARTE", "EN": "PART", "NL": "DEEL", "RU": "ЧАСТЬ", "TR": "BÖLÜM", "DE": "TEIL",
             "PT": "PARTE", "PL": "CZĘŚĆ", "CS": "ČÁST", "UK": "ЧАСТИНА", "ES": "PARTE", "RO": "PARTEA", "AR": "الجزء"}
POST_OUTRO = {
    "FR": "Merci d’avoir lu jusqu’ici.\nLa suite est dans le premier commentaire 👇",
    "IT": "Grazie per aver letto fin qui.\nIl seguito è nel primo commento 👇",
    "EN": "Thank you for reading this far.\nThe continuation is in the first comment 👇",
    "NL": "Bedankt dat je tot hier hebt gelezen.\nHet vervolg staat in de eerste reactie 👇",
    "RU": "Спасибо, что дочитали до этого места.\nПродолжение — в первом комментарии 👇",
    "TR": "Buraya kadar okuduğunuz için teşekkürler.\nDevamı ilk yorumda 👇",
    "DE": "Danke, dass Sie bis hierher gelesen haben.\nDie Fortsetzung finden Sie im ersten Kommentar 👇",
    "PT": "Obrigado por ler até aqui.\nA continuação está no primeiro comentário 👇",
    "PL": "Dziękuję, że dotarliście aż tutaj.\nDalszy ciąg czeka w pierwszym komentarzu 👇",
    "CS": "Děkuji, že jste dočetli až sem.\nPokračování najdete v prvním komentáři 👇",
    "UK": "Дякуємо, що дочитали до цього місця.\nПродовження — у першому коментарі 👇",
    "ES": "Gracias por leer hasta aquí.\nLa continuación está en el primer comentario 👇",
    "RO": "Vă mulțumim că ați citit până aici.\nContinuarea este în primul comentariu 👇",
    "AR": "شكرًا لأنكم قرأتم حتى هنا.\nالتتمة في التعليق الأول 👇",
}
COMMENT_OUTRO = {
    "FR": "Merci d’avoir lu jusqu’au bout ! Si vous voulez connaître la fin, aimez l’histoire et écrivez « Oui »",
    "IT": "Grazie per aver letto fino alla fine! Se volete sapere come finisce, mettete un like e scrivete «Sì»",
    "EN": "Thank you for reading to the end! If you want to know how it ends, like the story and write “Yes”",
    "NL": "Bedankt voor het lezen tot het einde! Wil je weten hoe het afloopt? Geef een like en schrijf „Ja”",
    "RU": "Спасибо, что дочитали до конца! Если хотите узнать финал, поставьте лайк и напишите «Да»",
    "TR": "Sonuna kadar okuduğunuz için teşekkürler! Sonunu öğrenmek istiyorsanız beğenin ve “Evet” yazın",
    "DE": "Danke, dass Sie bis zum Ende gelesen haben! Wenn Sie wissen möchten, wie es ausgeht, "
          "geben Sie ein Gefällt mir und schreiben Sie „Ja“",
    "PT": "Obrigado por ler até ao fim! Se quiser saber o final, deixe o seu like e escreva “Sim”",
    "PL": "Dziękuję, że przeczytaliście do końca! Jeśli chcecie poznać zakończenie, zostawcie polubienie "
          "i napiszcie „Tak”",
    "CS": "Děkuji, že jste dočetli až do konce! Pokud chcete znát konec, dejte like a napište „Ano“",
    "UK": "Дякуємо, що дочитали до кінця! Якщо хочете дізнатися фінал, поставте вподобайку й напишіть «Так»",
    "ES": "¡Gracias por leer hasta el final! Si quieres saber cómo termina, dale me gusta y escribe «Sí»",
    "RO": "Vă mulțumim că ați citit până la capăt! Dacă vreți să aflați finalul, dați like și scrieți „Da”",
    "AR": "شكرًا لأنكم قرأتم حتى النهاية! إذا أردتم معرفة النهاية، اضغطوا إعجاب واكتبوا «نعم»",
}


def _paras(text: str) -> list:
    """Абзацы: по пустым строкам; если их нет — по строкам."""
    text = (text or "").strip()
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(parts) <= 1:
        parts = [p.strip() for p in text.splitlines() if p.strip()]
    return parts


def _same_title(line: str, title: str) -> bool:
    norm = lambda s: re.sub(r"[\W_]+", "", (s or "").lower())  # noqa: E731
    return bool(line) and norm(line) == norm(title)


def parse_parts(raw: str) -> tuple:
    m = re.search(r"===TITLE===\s*(.*?)\s*===PART1===\s*(.*?)\s*===PART2===\s*(.*?)\s*===PART3===\s*(.*?)"
                  r"\s*(?:===END===|\Z)", raw or "", flags=re.S)
    if not m:
        raise GenerationError("ответ Claude не в нужном формате (нет маркеров ===TITLE===, ===PART1=== …)")
    title, p1, p2, p3 = (x.strip() for x in m.groups())
    title = title.splitlines()[0].strip() if title else ""
    title = title.strip(" \"'«»“”„*#").strip()
    clean = []
    for p in (p1, p2, p3):
        p = re.sub(r"<[^>]+>", "", p)                       # на всякий случай — без HTML
        p = re.sub(r"^```[a-z]*\s*|\s*```$", "", p, flags=re.I).strip()
        p = re.sub(r"\*+([^*\n]+?)\*+", r"\1", p).replace("*", "")   # без *курсива*: Facebook его не показывает
        clean.append(p)
    p1 = clean[0]
    first = p1.splitlines()[0] if p1 else ""
    if _same_title(first, title):                           # заголовок над постом ставим сами — без повтора
        p1 = p1[len(first):].strip()
    return title, p1, clean[1], clean[2]


def assemble_parts(lang: str, title: str, p1: str, p2: str, p3: str) -> tuple:
    """(текст поста, текст первого комментария без ссылки, HTML статьи)."""
    part = PART_WORD.get(lang, "PART")
    fb = f"{title}\n\n" + "\n\n".join(_paras(p1)) + f"\n\n{POST_OUTRO.get(lang, POST_OUTRO['EN'])}"
    comment = (f"{part} 2\n\n" + "\n\n".join(_paras(p2))
               + f"\n\n{COMMENT_OUTRO.get(lang, COMMENT_OUTRO['EN'])}\n\n{part} 3")
    body = lambda t: "\n".join(f"<p>{html.escape(x, quote=False)}</p>" for x in _paras(t))  # noqa: E731
    article = f"{body(p1)}\n<h2>{part} 2</h2>\n{body(p2)}\n<h2>{part} 3</h2>\n{body(p3)}"
    return fb, comment, article


def validate_parts(lang: str, source_text: str, title: str, p1: str, p2: str, p3: str) -> tuple:
    errors, warnings = [], []
    if not title or len(title) > 250:
        errors.append("заголовок пустой или длиннее 250 символов")
    w1, w2, w3 = words(p1), words(p2), words(p3)
    for name, w, lo, hi, soft in (("часть 1", w1, 250, 800, (330, 680)), ("часть 2", w2, 180, 700, (280, 530)),
                                  ("часть 3", w3, 300, 1000, (420, 750))):
        if not (lo <= w <= hi):
            errors.append(f"{name}: {w} слов")
        elif not (soft[0] <= w <= soft[1]):
            warnings.append(f"{name}: {w} слов")
    for name, txt in (("часть 1", p1), ("часть 2", p2), ("часть 3", p3)):
        if re.search(r"https?://|www\.", txt, flags=re.I):
            errors.append(f"{name}: есть ссылка")
        got = detect_language(txt)
        if got and got != lang:
            errors.append(f"язык ({name}) похож на {got}, а нужен {lang}")
    if not p1.rstrip().endswith(("…", "...")):
        warnings.append("часть 1 не обрывается на «…»")
    if re.search(r"\b(AI|ChatGPT|Claude|as an AI|translation)\b", " ".join((p1, p2, p3))):
        warnings.append("в тексте упоминается AI/перевод — проверьте")
    return errors, warnings


def words(text: str) -> int:
    return len(re.findall(r"\w+", re.sub(r"<[^>]+>", " ", text or "")))


def detect_language(text: str) -> str:
    low = (text or "").lower()
    tokens = re.findall(r"[^\W\d_]+", low)
    scores = {}
    for lang, sw in STOPWORDS.items():
        s = set(sw)
        scores[lang] = sum(1 for t in tokens if t in s)
    best = max(scores, key=scores.get) if tokens else ""
    if best in ("RU", "UK"):
        # у русского и украинского много общих коротких слов (в, не, на, я, у, за, так) — решают буквы:
        # і ї є ґ есть только в украинском, ы э ъ ё — только в русском (17.11: украинский текст принят за русский)
        uk, ru = len(re.findall(r"[іїєґ]", low)), len(re.findall(r"[ыэъё]", low))
        if uk or ru:
            return "UK" if uk > ru else "RU"
    return best


def sanitize_article(raw: str) -> str:
    text = raw.strip()
    text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.I)
    text = re.sub(r"<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>", "", text, flags=re.I | re.S)
    text = re.sub(r"<a\b[^>]*>(.*?)</a>", r"\1", text, flags=re.I | re.S)

    def keep(m):
        tag = m.group(2).lower()
        if tag in ALLOWED_TAGS:
            return f"<{m.group(1)}{tag}>"
        return ""

    text = re.sub(r"<\s*(/?)\s*([a-zA-Z0-9]+)[^>]*>", keep, text)
    text = re.sub(r"https?://\S+", "", text)
    if "<p>" not in text:
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        text = "\n".join(f"<p>{html.escape(p)}</p>" for p in paras)
    return text.strip()


def parse_output(raw: str) -> tuple:
    m = re.search(r"===TITLE===\s*(.*?)\s*===FACEBOOK===\s*(.*?)\s*===ARTICLE===\s*(.*?)\s*(?:===END===|\Z)",
                  raw or "", flags=re.S)
    if not m:
        raise GenerationError("ответ Claude не в нужном формате (нет маркеров ===TITLE=== и т.д.)")
    title, fb, art = (x.strip() for x in m.groups())
    title = title.splitlines()[0].strip() if title else ""
    title = title.strip(" \"'«»“”„*#").strip()
    return title, fb, art


def validate(lang: str, source_text: str, title: str, fb: str, article: str) -> tuple:
    """Возвращает (ошибки, предупреждения). Ошибки → перегенерация."""
    errors, warnings = [], []
    if not title or len(title) > 200:
        errors.append("заголовок пустой или длиннее 200 символов")
    if not fb.strip():
        errors.append("пустой текст для Facebook")
    if re.search(r"https?://|www\.", fb, flags=re.I):
        errors.append("в тексте для Facebook есть ссылка")
    src_w, fb_w, art_w = words(source_text), words(fb), words(article)
    if src_w >= 40 and not (0.55 * src_w <= fb_w <= 1.6 * src_w):
        errors.append(f"длина текста FB {fb_w} слов при исходных {src_w}")
    elif src_w >= 40 and not (0.85 * src_w <= fb_w <= 1.15 * src_w):
        warnings.append(f"длина текста FB {fb_w} слов (исходник {src_w}, цель ±10%)")
    if art_w < 450 or art_w > 1300:
        errors.append(f"длина статьи {art_w} слов (нужно около 700–800)")
    elif not (600 <= art_w <= 950):
        warnings.append(f"длина статьи {art_w} слов")
    for name, txt in (("FB", fb), ("статья", article)):
        got = detect_language(txt)
        if got and got != lang:
            errors.append(f"язык ({name}) похож на {got}, а нужен {lang}")
    if re.search(r"\b(AI|ChatGPT|Claude|as an AI|translation)\b", fb + " " + article):
        warnings.append("в тексте упоминается AI/перевод — проверьте")
    return errors, warnings


class Generator:
    def __init__(self, cfg, backend: str | None = None):
        self.cfg = cfg
        c = cfg.claude
        self.backend = backend or os.environ.get("CLAUDE_BACKEND") or c.get("backend", "auto")
        self.model = c.get("model", "claude-sonnet-5-5")
        self.max_tokens = int(c.get("max_tokens", 8000))
        self.max_attempts = int(c.get("max_attempts", 3))
        self.system = cfg.path("prompts/system.md").read_text(encoding="utf-8").strip()
        # classic — вся история в посте, в комментарии только ссылка; parts — пост / комментарий / статья по частям
        # свой формат для отдельных групп (settings: post_format_by_group), иначе общий post_format
        by_group = cfg.settings.get("post_format_by_group") or {}
        self.post_format = str(by_group.get(getattr(cfg, "group", "") or "")
                               or cfg.settings.get("post_format") or "classic")
        tpl = "prompts/generate_parts.md" if self.post_format == "parts" else "prompts/generate.md"
        self.template = cfg.path(tpl).read_text(encoding="utf-8")
        self.log = []
        self.last_usage = None  # расход токенов последнего обращения к Claude (для теста стоимости)

    # --- публичный метод -------------------------------------------------------
    def generate(self, lang: str, source_text: str) -> Generated:
        src_w = words(source_text)
        prompt = (self.template.replace("{LANGUAGE}", self.cfg.lang_name(lang))
                  .replace("{SOURCE_WORDS}", str(src_w))
                  .replace("{PART1_WORDS}", str(min(600, max(400, src_w))))
                  .replace("{SOURCE}", source_text.strip()))
        last_err = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                raw, used = self._complete(prompt, lang)
                if self.post_format == "parts":
                    title, p1, p2, p3 = parse_parts(raw)
                    errors, warnings = validate_parts(lang, source_text, title, p1, p2, p3)
                    if errors:
                        raise GenerationError("; ".join(errors))
                    fb, comment, article = assemble_parts(lang, title, p1, p2, p3)
                    return Generated(title, fb, article, used, self.model, warnings, comment_text=comment)
                title, fb, art_raw = parse_output(raw)
                article = sanitize_article(art_raw)
                errors, warnings = validate(lang, source_text, title, fb, article)
                if errors:
                    raise GenerationError("; ".join(errors))
                return Generated(title, fb.strip(), article, used, self.model, warnings)
            except GenerationError as e:
                last_err = e
                self.log.append(f"{lang} попытка {attempt}: {e}")
        raise GenerationError(f"{lang}: не удалось получить корректный текст за {self.max_attempts} попытки: {last_err}")

    def ping(self) -> str:
        """Короткая проверка доступа к Claude. Возвращает название бэкенда."""
        raw, used = self._complete("Reply with exactly one word: OK", "EN")
        if used != "mock" and "OK" not in raw.upper():
            raise GenerationError(f"неожиданный ответ: {raw[:80]}")
        return used

    # --- бэкенды ---------------------------------------------------------------
    def _complete(self, prompt: str, lang: str) -> tuple:
        b = self.backend
        if b == "mock":
            return _mock_output(lang, prompt), "mock"
        if b == "subscription":
            return self._cli(prompt), "subscription"
        if b == "api":
            return self._api(prompt), "api"
        if b == "auto":
            if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
                try:
                    return self._cli(prompt), "subscription"
                except GenerationError as e:
                    if not os.environ.get("ANTHROPIC_API_KEY"):
                        raise
                    self.log.append(f"подписка недоступна ({e}); переключаюсь на API")
            if os.environ.get("ANTHROPIC_API_KEY"):
                return self._api(prompt), "api"
            raise GenerationError("не задан ни CLAUDE_CODE_OAUTH_TOKEN, ни ANTHROPIC_API_KEY")
        raise GenerationError(f"неизвестный backend '{b}'")

    def _cli(self, prompt: str) -> str:
        exe = ensure_claude_cli()
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)  # чтобы CLI точно использовал подписку
        cmd = [exe, "-p", "--output-format", "json", "--model", self.model,
               "--system-prompt", self.system, "--tools", "", "--max-turns", "1",
               "--no-session-persistence", "--disable-slash-commands"]
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=env,
                                  timeout=300, cwd=str(self.cfg.root / "state"))
        except subprocess.TimeoutExpired:
            raise GenerationError("Claude CLI: таймаут 5 минут")
        out = proc.stdout.strip()
        try:
            body = json.loads(out.splitlines()[-1] if out else "{}")
        except json.JSONDecodeError:
            raise GenerationError(f"Claude CLI: непонятный ответ (код {proc.returncode}): {proc.stderr[-300:]}")
        if body.get("is_error") or proc.returncode != 0:
            raise GenerationError(f"Claude CLI: {str(body.get('result') or proc.stderr)[-300:]}")
        self.last_usage = {"usage": body.get("usage"), "modelUsage": body.get("modelUsage"),
                           "total_cost_usd": body.get("total_cost_usd"), "duration_ms": body.get("duration_ms")}
        return body.get("result", "")

    def _api(self, prompt: str) -> str:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise GenerationError("ANTHROPIC_API_KEY не задан")
        payload = {"model": self.model, "max_tokens": self.max_tokens, "system": self.system,
                   "messages": [{"role": "user", "content": prompt}]}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        for attempt in range(1, 5):
            try:
                r = requests.post("https://api.anthropic.com/v1/messages", headers=headers,
                                  json=payload, timeout=600)
            except requests.RequestException as e:
                err = f"сеть: {e}"
            else:
                if r.status_code == 200:
                    body = r.json()
                    self.last_usage = {"usage": body.get("usage"), "modelUsage": None,
                                       "total_cost_usd": None, "duration_ms": None}
                    if body.get("stop_reason") == "max_tokens":
                        raise GenerationError("ответ обрезан (max_tokens)")
                    return "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
                err = f"HTTP {r.status_code}: {r.text[:300]}"
                if r.status_code not in (429, 500, 502, 503, 529):
                    raise GenerationError(f"Claude API {err}")
            time.sleep(10 * attempt)
        raise GenerationError(f"Claude API недоступен: {err}")


# --- тестовые тексты для сухого прогона ---------------------------------------
_MOCK = {
    "FR": ("Une rencontre qui a tout changé", "Elle ne savait pas que ce jour-là allait changer sa vie. ",
           "Continuez la lecture dans le premier commentaire 👇"),
    "IT": ("Un incontro che ha cambiato tutto", "Lei non sapeva che quel giorno le avrebbe cambiato la vita. ",
           "Continua a leggere nel primo commento 👇"),
    "EN": ("A meeting that changed everything", "She did not know that this day was going to change her life. ",
           "Continue reading in the first comment 👇"),
    "NL": ("Een ontmoeting die alles veranderde", "Ze wist niet dat die dag haar leven zou veranderen en dat het mooi was. ",
           "Lees verder in de eerste reactie 👇"),
    "RU": ("Встреча, которая всё изменила", "Она не знала, что этот день изменит её жизнь, и это было так. ",
           "Продолжение читайте в первом комментарии 👇"),
    "TR": ("Her şeyi değiştiren bir karşılaşma", "O gün onun için her şey değişti ve bu çok güzel bir şey oldu. ",
           "Devamı ilk yorumda 👇"),
    "PT": ("Um encontro que mudou tudo", "Ela não sabia que aquele dia ia mudar a sua vida, e foi muito bom. ",
           "Continue lendo no primeiro comentário 👇"),
    "PL": ("Spotkanie, które zmieniło wszystko", "Nie wiedziała, że ten dzień zmieni jej życie, i było to bardzo dobre. ",
           "Czytaj dalej w pierwszym komentarzu 👇"),
    "CS": ("Setkání, které všechno změnilo", "Nevěděla, že ten den jí změní život, a bylo to velmi dobré. ",
           "Pokračování v prvním komentáři 👇"),
    "DE": ("Eine Begegnung, die alles veränderte", "Sie wusste nicht, dass dieser Tag ihr Leben verändern würde, und es war gut. ",
           "Weiterlesen im ersten Kommentar 👇"),
    "UK": ("Зустріч, яка змінила все", "Вона не знала, що цей день змінить її життя, і це було дуже добре. ",
           "Продовження читайте в першому коментарі 👇"),
    "ES": ("Un encuentro que lo cambió todo", "Ella no sabía que aquel día iba a cambiar su vida, y fue muy bonito. ",
           "Sigue leyendo en el primer comentario 👇"),
    "RO": ("O întâlnire care a schimbat totul", "Ea nu știa că acea zi îi va schimba viața, și a fost foarte frumos. ",
           "Continuarea în primul comentariu 👇"),
    "AR": ("لقاء غيّر كل شيء", "لم تكن تعلم أن هذا اليوم سيغير حياتها، وكان ذلك جميلا جدا. ",
           "تابع القراءة في التعليق الأول 👇"),
}


def _mock_output(lang: str, prompt: str) -> str:
    title, sentence, outro = _MOCK[lang]
    if "===PART1===" in prompt:
        m = re.search(r"About (\d+) words", prompt)
        n1 = int(m.group(1)) if m else 450
        per = max(1, words(sentence))
        para = lambda n: "\n\n".join((sentence * 3).strip() for _ in range(max(1, round(n / per / 3))))  # noqa: E731
        return (f"===TITLE===\n{title}\n===PART1===\n{para(n1)}…\n===PART2===\n{para(400)}…\n"
                f"===PART3===\n{para(550)}\n===END===")
    m = re.search(r"about (\d+) words", prompt)
    src_words = int(m.group(1)) if m else 120
    per = max(1, words(sentence))
    fb = (sentence * max(1, round(src_words / per) - 1)).strip() + " " + outro
    article = "\n".join(f"<p>{sentence * 8}</p>" for _ in range(6))
    return f"===TITLE===\n{title}\n===FACEBOOK===\n{fb}\n===ARTICLE===\n{article}\n===END==="
