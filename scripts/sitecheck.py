#!/usr/bin/env python3
"""Техническая база сайта для поиска и нейросетей: факты, а не оценки.

Открывает сайт так, как его видит робот без JavaScript, и печатает, что на месте,
а что мешает поисковикам и AI-краулерам. Скоров не ставит: каждая строка это
проверяемый факт со страницы. Что с этим делать, решает режим «настройка» скилла
(references/setup-checklist.md).

    sitecheck.py                         сайт из настроек: главная и до 10 страниц из карты
    sitecheck.py https://example.ru      любой сайт, настройки не нужны
    sitecheck.py --pages 25              больше страниц из карты сайта
    sitecheck.py /uslugi/ /blog/post/    главная и указанные страницы
    sitecheck.py --json                  то же в JSON

Что смотрит:
  сайт       https и редиректы, www, ответ на несуществующий адрес, robots.txt, карта сайта
  краулеры   кому robots.txt разрешает главную: поисковики и роботы нейросетей
  страницы   код ответа, title, description, h1, canonical, noindex, язык, разметка
             schema.org, Open Graph, счетчики, объем текста без JavaScript

Ничего не меняет: только читает публичные страницы.
"""
import argparse
import json
import re
import sys
import time
import urllib.robotparser
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _site  # noqa: E402

import requests  # noqa: E402

UA = "Mozilla/5.0 (compatible; seo-geo-agent-sitecheck/1.0)"
TIMEOUT = 25

# Робот -> чей он и что дает. Порядок это порядок в отчете.
CRAWLERS = [
    ("Yandex", "Яндекс: поиск и ответы Алисы"),
    ("Googlebot", "Google: поиск, AI Overviews и AI Mode"),
    ("bingbot", "Bing: поиск и Copilot"),
    ("OAI-SearchBot", "ChatGPT: поисковый индекс"),
    ("ChatGPT-User", "ChatGPT: открывает страницу по ходу ответа"),
    ("GPTBot", "OpenAI: обучение моделей"),
    ("ClaudeBot", "Anthropic: обучение моделей"),
    ("Claude-SearchBot", "Claude: поисковый индекс"),
    ("Claude-User", "Claude: открывает страницу по ходу ответа"),
    ("PerplexityBot", "Perplexity: поисковый индекс"),
    ("Perplexity-User", "Perplexity: открывает страницу по ходу ответа"),
    ("Google-Extended", "Google: обучение Gemini (на поиск не влияет)"),
    ("YandexAdditional", "Яндекс: использование в ответах нейросетей"),
    ("CCBot", "Common Crawl: открытый корпус для обучения"),
]
# Без этих роботов сайта не будет в поиске или в ответах соответствующей нейросети.
ESSENTIAL = {"Yandex", "Googlebot", "bingbot", "OAI-SearchBot", "ChatGPT-User", "Claude-User",
             "Claude-SearchBot", "PerplexityBot", "Perplexity-User"}


class Page(HTMLParser):
    """Собирает со страницы то, что читает поисковый робот."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.h1, self.meta, self.links, self.ld, self.text = "", [], {}, {}, [], []
        self.lang = None
        self._in = []
        self._buf = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang")
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or a.get("http-equiv") or "").lower()
            if name and name not in self.meta:
                self.meta[name] = a.get("content", "").strip()
        elif tag == "link":
            rel = a.get("rel", "").lower()
            if rel and rel not in self.links:
                self.links[rel] = a.get("href", "").strip()
        if tag in ("title", "h1", "script", "style", "noscript", "template", "svg"):
            self._in.append((tag, a.get("type", "").lower()))
            self._buf = []

    def handle_endtag(self, tag):
        if self._in and self._in[-1][0] == tag:
            _, kind = self._in.pop()
            body = "".join(self._buf).strip()
            if tag == "title" and not self.title:
                self.title = re.sub(r"\s+", " ", body)
            elif tag == "h1":
                self.h1.append(re.sub(r"\s+", " ", body))
            elif tag == "script" and "ld+json" in kind:
                self.ld.append(body)
            self._buf = []

    def handle_data(self, data):
        if self._in:
            self._buf.append(data)
            if self._in[-1][0] == "h1":
                self.text.append(data)
        else:
            self.text.append(data)


def get(url, **kw):
    try:
        return requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA}, **kw)
    except requests.RequestException as e:
        return e


def ld_types(blocks):
    types, broken = [], 0

    def walk(x):
        if isinstance(x, dict):
            t = x.get("@type")
            if t:
                types.extend(t if isinstance(t, list) else [t])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    for b in blocks:
        try:
            walk(json.loads(b))
        except ValueError:
            broken += 1
    return sorted(set(map(str, types))), broken


def check_page(url):
    t0 = time.time()
    r = get(url)
    if isinstance(r, Exception):
        return {"url": url, "error": f"не открылась: {type(r).__name__}"}
    p = Page()
    try:
        p.feed(r.text)
    except Exception:  # noqa: BLE001
        pass
    html = r.text
    words = len(re.findall(r"\w{2,}", " ".join(p.text)))
    canonical = urljoin(r.url, p.links["canonical"]) if p.links.get("canonical") else None
    robots_meta = (p.meta.get("robots", "") + " " + r.headers.get("X-Robots-Tag", "")).lower()
    types, broken = ld_types(p.ld)
    counters = [name for name, rx in (
        ("Яндекс.Метрика", r"mc\.yandex\.(ru|com)/(metrika|watch)|ym\(\s*\d+"),
        ("Google Analytics", r"googletagmanager\.com/gtag/js|google-analytics\.com/(analytics|g/collect)"),
        ("Google Tag Manager", r"googletagmanager\.com/gtm\.js"),
    ) if re.search(rx, html)]
    return {
        "url": url, "final": r.url, "status": r.status_code, "seconds": round(time.time() - t0, 2),
        "kb": round(len(r.content) / 1024), "title": p.title, "description": p.meta.get("description", ""),
        "h1": p.h1, "canonical": canonical, "noindex": "noindex" in robots_meta, "lang": p.lang,
        "viewport": "viewport" in p.meta, "og": [k for k in ("og:title", "og:description", "og:image") if p.meta.get(k)],
        "schema": types, "schema_broken": broken, "counters": counters, "words": words,
        "verification": [k for k in ("yandex-verification", "google-site-verification") if p.meta.get(k)],
    }


def same_page(a, b):
    def norm(u):
        x = urlparse(u)
        return (x.netloc.lower().removeprefix("www."), x.path.rstrip("/") or "/")
    return norm(a) == norm(b)


def sitemap_urls(start, seen=None, depth=0):
    """Адреса из карты сайта; индекс карт раскрывается на один уровень вглубь."""
    seen = seen if seen is not None else set()
    if start in seen or depth > 2 or len(seen) > 30:
        return []
    seen.add(start)
    r = get(start)
    if isinstance(r, Exception) or r.status_code != 200:
        return []
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)
    if "<sitemapindex" in r.text[:2000]:
        out = []
        for loc in locs[:20]:
            out += sitemap_urls(loc, seen, depth + 1)
        return out
    return locs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="*", help="адрес сайта и/или пути страниц")
    ap.add_argument("--pages", type=int, default=10, help="сколько страниц взять из карты сайта")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    base = next((t for t in args.targets if t.startswith("http")), None)
    paths = [t for t in args.targets if not t.startswith("http")]
    if not base:
        base = _site.site_url()
    base = base.rstrip("/")
    host = urlparse(base).netloc
    bare = host.removeprefix("www.")
    notes = []          # (уровень, текст): "!!" мешает поиску, "! " стоит поправить, "ok" на месте

    def note(level, text):
        notes.append((level, text))

    # ---- сайт целиком
    home = get(base + "/")
    if isinstance(home, Exception):
        sys.exit(f"Сайт не открылся: {base} ({type(home).__name__})")
    final_host = urlparse(home.url).netloc
    if not home.url.startswith("https://"):
        note("!!", f"главная открывается без https: {home.url}")
    plain = get(f"http://{bare}/", allow_redirects=False)
    if not isinstance(plain, Exception):
        if plain.status_code in (301, 308) and plain.headers.get("Location", "").startswith("https://"):
            note("ok", "http перенаправляет на https постоянным редиректом")
        elif plain.status_code in (302, 303, 307):
            note("! ", f"http перенаправляет на https временным редиректом ({plain.status_code}): нужен 301")
        elif plain.status_code == 200:
            note("!!", "сайт открывается и по http, и по https: это дубли, нужен редирект 301 на https")
    other = ("www." + bare) if not final_host.startswith("www.") else bare
    twin = get(f"https://{other}/", allow_redirects=False)
    if not isinstance(twin, Exception) and twin.status_code == 200:
        note("! ", f"сайт отвечает и на {final_host}, и на {other} без редиректа: зеркала лучше склеить")
    ghost = get(f"{base}/seo-geo-agent-check-{int(time.time())}/")
    if not isinstance(ghost, Exception):
        if ghost.status_code == 200:
            note("!!", "несуществующий адрес отвечает 200 вместо 404: поисковики будут индексировать мусор")
        else:
            note("ok", f"несуществующий адрес отвечает {ghost.status_code}")

    # ---- robots.txt и краулеры
    robots = get(base + "/robots.txt")
    access, sitemaps = [], []
    if isinstance(robots, Exception) or robots.status_code != 200 or "<html" in robots.text[:300].lower():
        code = "нет ответа" if isinstance(robots, Exception) else robots.status_code
        note("! ", f"robots.txt не найден ({code}): роботам разрешено все, но карту сайта им никто не подсказал")
        access = [(name, what, True) for name, what in CRAWLERS]
    else:
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(robots.text.splitlines())
        sitemaps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots.text)
        access = [(name, what, rp.can_fetch(name, base + "/")) for name, what in CRAWLERS]
        blocked = [name for name, _, ok in access if not ok]
        hard = [n for n in blocked if n in ESSENTIAL]
        if hard:
            note("!!", "robots.txt закрывает главную для: " + ", ".join(hard))
        elif blocked:
            note("! ", "robots.txt закрывает главную для: " + ", ".join(blocked) + " (на поиск не влияет, решение владельца)")
        else:
            note("ok", "robots.txt никого из проверенных роботов не закрывает")
        if not sitemaps:
            note("! ", "в robots.txt нет строки Sitemap")

    # ---- карта сайта
    candidates = sitemaps or [base + "/sitemap.xml"]
    urls = []
    for sm in candidates[:3]:
        urls += sitemap_urls(sm)
    urls = list(dict.fromkeys(urls))
    if urls:
        foreign = [u for u in urls if urlparse(u).netloc.removeprefix("www.") != bare]
        note("ok", f"карта сайта: {len(urls)} адресов ({candidates[0]})")
        if foreign:
            note("! ", f"в карте сайта {len(foreign)} адресов чужого домена, например {foreign[0]}")
        if any(u.startswith("http://") for u in urls):
            note("! ", "в карте сайта есть адреса с http")
    else:
        note("!!", f"карта сайта не найдена или пуста ({candidates[0]}): новые страницы роботы находят медленно")
    llms = get(base + "/llms.txt")
    if getattr(llms, "status_code", 0) == 200 and "<html" not in llms.text[:300].lower():
        note("ok", "llms.txt есть (на видимость по данным исследований не влияет, вреда нет)")

    # ---- страницы
    todo = [base + "/"] + [base + (p if p.startswith("/") else "/" + p) for p in paths]
    rest = [u for u in urls if not same_page(u, base + "/")]
    if rest and args.pages > 0:
        step = max(1, len(rest) // args.pages)
        todo += rest[::step][:args.pages]
    todo = list(dict.fromkeys(todo))
    pages = [check_page(u) for u in todo]

    for p in pages:
        path = urlparse(p["url"]).path or "/"
        if p.get("error"):
            note("!!", f"{path}: {p['error']}")
            continue
        if p["status"] != 200:
            note("!!", f"{path}: отвечает {p['status']}" + (" (и стоит в карте сайта)" if p["url"] in urls else ""))
            continue
        if not same_page(p["url"], p["final"]):
            note("! ", f"{path}: перенаправляет на {p['final']}" + (", а в карте сайта стоит старый адрес" if p["url"] in urls else ""))
        if p["noindex"]:
            note("!!", f"{path}: закрыта от индексации (noindex)" + (", хотя стоит в карте сайта" if p["url"] in urls else ""))
        if not p["title"]:
            note("!!", f"{path}: нет title")
        elif len(p["title"]) > 70:
            note("! ", f"{path}: title длиной {len(p['title'])} знаков, в выдаче обрежется")
        if not p["description"]:
            note("! ", f"{path}: нет meta description")
        if len(p["h1"]) != 1:
            note("! ", f"{path}: заголовков h1 {len(p['h1'])}, нужен один")
        if not p["canonical"]:
            note("! ", f"{path}: нет canonical")
        elif not same_page(p["canonical"], p["final"]):
            note("! ", f"{path}: canonical указывает на другую страницу: {p['canonical']}")
        if p["words"] < 120:
            note("!!", f"{path}: без JavaScript на странице {p['words']} слов. Похоже, текст рисует скрипт: "
                       "поисковики видят его хуже, а роботы нейросетей могут не увидеть вовсе")
        if p["schema_broken"]:
            note("! ", f"{path}: разметка JSON-LD не разбирается ({p['schema_broken']} блок)")
        if not p["viewport"]:
            note("! ", f"{path}: нет meta viewport, на телефоне страница будет мелкой")
        if p["seconds"] > 3:
            note("! ", f"{path}: HTML отдается {p['seconds']} с")

    good = [p for p in pages if not p.get("error") and p["status"] == 200]
    for field, label in (("title", "title"), ("description", "description")):
        dup = [v for v, n in Counter(p[field] for p in good if p[field]).items() if n > 1]
        if dup:
            note("! ", f"одинаковый {label} у нескольких страниц: «{dup[0][:60]}»")
    home_page = good[0] if good else {}
    if home_page:
        if home_page["counters"]:
            note("ok", "счетчики на главной: " + ", ".join(home_page["counters"]))
        else:
            note("! ", "счетчиков в HTML главной не видно (если они ставятся через менеджер тегов, это нормально)")
        if not home_page["schema"]:
            note("! ", "на главной нет разметки schema.org (Organization или LocalBusiness помогает с карточкой компании)")
        if not home_page["lang"]:
            note("! ", "у тега html нет атрибута lang")
        if len(home_page["og"]) < 3:
            note("! ", "на главной неполный Open Graph: ссылка в мессенджере и соцсети будет без картинки или описания")

    if args.json:
        print(json.dumps({"site": base, "robots_access": [{"bot": n, "allowed": ok} for n, _, ok in access],
                          "sitemap_urls": len(urls), "pages": pages,
                          "notes": [{"level": l.strip(), "text": t} for l, t in notes]}, ensure_ascii=False, indent=1))
        return

    print(f"\nТехническая база: {base}   (проверено страниц: {len(pages)}, в карте сайта: {len(urls)})\n")
    print("Кому robots.txt разрешает главную")
    for name, what, ok in access:
        print(f"  {'да ' if ok else 'НЕТ'}  {name:18} {what}")
    print("\nСтраницы")
    for p in pages:
        path = (urlparse(p["url"]).path or "/")[:44]
        if p.get("error"):
            print(f"  {path:44}  {p['error']}")
            continue
        print(f"  {path:44}  {p['status']}  слов {p['words']:5}  h1 {len(p['h1'])}  "
              f"{'canonical' if p['canonical'] else 'БЕЗ canonical'}  "
              f"разметка: {', '.join(p['schema'][:4]) or 'нет'}")
        print(f"      title ({len(p['title'])}): {p['title'][:90]}")
        print(f"      description ({len(p['description'])}): {p['description'][:110] or '—'}")
    for level, title in (("!!", "Мешает поиску и нейросетям"), ("! ", "Стоит поправить"), ("ok", "На месте")):
        rows = [t for l, t in notes if l == level]
        if rows:
            print(f"\n{title}")
            for t in rows:
                print(f"  - {t}")
    print("\nЧего скрипт не видит: скорость на телефоне (Lighthouse), подтверждение сайта в Вебмастере и")
    print("Search Console, цели в счетчике, качество текстов. Это шаги references/setup-checklist.md.\n")


if __name__ == "__main__":
    main()
