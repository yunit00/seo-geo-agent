#!/usr/bin/env python3
"""Кандидаты для рекомендаций: какой материал написать и какую страницу доработать.

    opportunities.py [папка снимка] [--no-api]

Это не решение, а отсортированный список с цифрами. Решение принимает человек
в разговоре с агентом (режим «совет» скилла): правила не видят, есть ли владельцу
что сказать по теме, не устарел ли повод и не занят ли запрос чужим намерением.

Что написать. Кандидаты берутся из двух мест:
  1) candidates.txt в рабочей папке сайта: темы, которые уже приходили в голову
     (фраза | заметка);
  2) Search Console за 90 дней: запросы, по которым Google показывает общую страницу
     (главную или раздел из hub_pages в site.json). Раз показывает общую страницу,
     отдельной под запрос на сайте нет.
Для каждого снимается спрос (Wordstat, запросов в месяц со словами фразы) и топ-10
Яндекса. Выдача считается по числу крупных сайтов в топ-10 (встроенный список плюс
strong_domains в site.json): до 3 слабая, 4-6 средняя, от 7 сильная. Оценка это
спрос, умноженный на долю некрупных сайтов; при сильной выдаче она режется втрое.

Что доработать. Сигналы собираются по страницам:
  - запрос из трекера позиций стоит ниже 10-го места в Яндексе или вне первых 50;
  - запрос в Google стоит на 8-20 месте и уже получает показы;
  - входы из поиска на страницу упали за 28 дней (нужны логи сервера).
Оценка страницы это сумма потенциала сигналов: спрос запроса (не больше 20 тысяч)
с поправкой на место (11-20 весит 0.6, 21-30 0.35, 31-50 0.15, вне первых 50 всего
0.03), показы в Google за месяц, потерянные входы. Близость к первой странице важнее
размера спроса: широкий запрос, по которому сайта нет в первых 50, доработкой не взять.

Деньги. Запросы Wordstat и выдачи платные (тарифы Yandex Search API). Ответы
кэшируются на 30 дней, поэтому регулярный запуск почти ничего не тратит. За один
запуск скрипт делает не больше 70 новых запросов Wordstat (у него почасовая квота)
и 25 новых запросов выдачи. Что не поместилось, доснимется в следующий запуск,
а в выводе написано, сколько фраз осталось без цифры. С ключом --no-api или без
подключенного Wordstat берется только кэш.
"""
import base64
import datetime as dt
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _site  # noqa: E402
import wordstat  # noqa: E402

LINKED = _site.connected()
args = [a for a in sys.argv[1:] if not a.startswith("--")]     # после импорта: _site уже убрал --site
NO_API = "--no-api" in sys.argv or not LINKED["wordstat"]
SNAP = Path(args[0]) if args else None
REGION = wordstat.REGION
WORDSTAT_MAX, SERP_MAX = 70, 25
DOMAIN = _site.DOMAIN
NOISE = [re.compile(x, re.I) for x in _site.SITE.get("noise_queries", [])]
BRAND = [re.compile(x, re.I) for x in _site.SITE.get("brand_queries", [])]
HUBS = set(_site.SITE.get("hub_pages") or ["/"])
# Крупные сайты, с которыми трудно спорить в выдаче. Лидеры своей ниши добавляются
# в site.json, ключ strong_domains: они дополняют этот список, а не заменяют его.
STRONG = tuple({
    "wikipedia.org", "yandex.ru", "ya.ru", "dzen.ru", "google.com", "youtube.com", "rutube.ru", "vk.com",
    "ok.ru", "mail.ru", "habr.com", "vc.ru", "pikabu.ru", "t-j.ru", "tbank.ru", "tinkoff.ru", "sber.ru",
    "sberbank.ru", "banki.ru", "sravni.ru", "ozon.ru", "wildberries.ru", "avito.ru", "market.yandex.ru",
    "aliexpress.ru", "dns-shop.ru", "mvideo.ru", "citilink.ru", "leroymerlin.ru", "lemanapro.ru", "2gis.ru",
    "hh.ru", "gosuslugi.ru", "consultant.ru", "garant.ru", "nalog.gov.ru", "rbc.ru", "kommersant.ru",
    "forbes.ru", "lenta.ru", "ria.ru", "tass.ru", "kp.ru", "championat.com", "ixbt.com", "3dnews.ru",
    "cnews.ru", "mts.ru", "microsoft.com", "apple.com", "github.com", "irecommend.ru", "otzovik.com",
    *(d.lower() for d in _site.SITE.get("strong_domains", []))})
spent = {"wordstat": 0, "serp": 0, "noFreq": 0, "noSerp": 0}


def fresh(kind, phrase):
    p = wordstat.cache_path(kind, phrase, REGION)
    if not p.exists():
        return False
    return (dt.date.today() - dt.date.fromisoformat(json.loads(p.read_text())["date"])).days <= wordstat.CACHE_DAYS


def freq(phrase):
    """Запросов в месяц со словами фразы или None, если цифры нет и снять ее сейчас нельзя."""
    hit = fresh("top1", phrase)
    if not hit and (NO_API or spent["wordstat"] >= WORDSTAT_MAX):
        spent["noFreq"] += 1
        return None
    try:
        data, _ = wordstat.get_top(phrase, 1, REGION)
    except (SystemExit, Exception):  # noqa: BLE001
        spent["noFreq"] += 1
        return None
    if not hit:
        spent["wordstat"] += 1
        time.sleep(0.15)
    return wordstat.num(data.get("totalCount"))


def serp10(phrase):
    """Топ-10 Яндекса или None."""
    hit = fresh("serp10", phrase)
    if not hit and (NO_API or spent["serp"] >= SERP_MAX):
        spent["noSerp"] += 1
        return None

    def fetch():
        j = wordstat.post("/v2/web/search", {
            "query": {"searchType": "SEARCH_TYPE_RU", "queryText": phrase},
            "groupSpec": {"groupsOnPage": 10}, "region": REGION, "responseFormat": "FORMAT_XML"})
        return wordstat.parse_serp(base64.b64decode(j["rawData"]).decode("utf-8", "replace"))
    try:
        docs, _ = wordstat.cached("serp10", phrase, REGION, fetch, False)
    except (SystemExit, Exception):  # noqa: BLE001
        spent["noSerp"] += 1
        return None
    if not hit:
        spent["serp"] += 1
        time.sleep(1.1)       # лимит API: один запрос в секунду
    return docs


def is_strong(domain):
    d = domain.lower().removeprefix("www.")
    return any(d == s or d.endswith("." + s) for s in STRONG)


def skip(q):
    return any(rx.search(q) for rx in NOISE + BRAND)


def short(url):
    return "главная" if url == "/" else url


def num(n):
    return f"{n:,}".replace(",", " ")


# ---------- Search Console: одна выгрузка «страница + запрос» за 90 дней
gsc_rows, gsc_error = [], None
try:
    if not LINKED["gsc"]:
        raise RuntimeError("Search Console не подключен")
    import gsc
    api = gsc.service()
    site = gsc.site_url()
    end = dt.date.today() - dt.timedelta(days=3)
    body = {"startDate": (end - dt.timedelta(days=89)).isoformat(), "endDate": end.isoformat(),
            "dimensions": ["page", "query"], "rowLimit": 5000}
    for r in api.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", []):
        page = gsc.path_of(r["keys"][0])
        gsc_rows.append({"page": page, "query": r["keys"][1], "impressions": r["impressions"],
                         "clicks": r["clicks"], "position": r["position"]})
except (SystemExit, Exception) as e:  # noqa: BLE001
    gsc_error = str(e)[:160] or type(e).__name__

# ---------- что написать
cands = {}
kfile = _site.WORKDIR / "candidates.txt"
if kfile.exists():
    for line in kfile.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            q, _, note = (p.strip() for p in line.partition("|"))
            cands[q.lower()] = {"topic": q, "source": "список тем", "note": note}
for r in sorted(gsc_rows, key=lambda r: -r["impressions"]):
    q = r["query"].lower()
    if r["page"] in HUBS and r["impressions"] >= 40 and not skip(q) and q not in cands and "http" not in q:
        cands[q] = {"topic": r["query"], "source": "Google показывает общую страницу",
                    "note": f"{round(r['impressions'])} показов за 90 дней на странице «{short(r['page'])}», место {r['position']:.0f}"}

write = []
for c in sorted(cands.values(), key=lambda c: c["source"] != "список тем"):
    f = freq(c["topic"])
    docs = serp10(c["topic"]) if f is None or f >= 100 else None      # на мелкий спрос выдачу не тратим
    item = {**c, "freq": f, "strong": None, "verdict": None, "top": [], "ours": None, "score": 0}
    if docs is not None:
        strong = sum(1 for d in docs[:10] if is_strong(d["domain"]))
        ours = next((i for i, d in enumerate(docs[:10], 1) if DOMAIN in d["domain"]), None)
        item.update(strong=strong, ours=ours, top=[d["domain"] for d in docs[:3]],
                    verdict="слабая" if strong <= 3 else ("средняя" if strong <= 6 else "сильная"))
        # Первым стоит сайт самого продукта: запрос отчасти навигационный, часть спроса уйдет туда.
        first_word = re.sub(r"[^a-zа-я0-9]", "", c["topic"].lower().split()[0])
        item["nav"] = bool(docs) and len(first_word) >= 4 and first_word in docs[0]["domain"].lower()
        if f:
            item["score"] = round(f * (10 - strong) / 10 * (0.33 if strong >= 7 else 1) * (0.2 if ours else 1)
                                  * (0.5 if item["nav"] else 1))
    parts = []
    if f is not None:
        parts.append(f"{num(f)} запросов в месяц")
    if item["verdict"]:
        parts.append(f"выдача {item['verdict']}: крупных сайтов в топ-10 Яндекса {item['strong']}" +
                     (f", первым стоит {item['top'][0]}" if item["top"] else ""))
    if item.get("nav"):
        parts.append("первым стоит сайт самого продукта, часть спроса идет туда")
    if item["ours"]:
        parts.append(f"мы уже на {item['ours']}-м месте, это скорее доработка")
    if c["note"]:
        parts.append(c["note"])
    item["why"] = "; ".join(parts) if parts else "цифр пока нет"
    write.append(item)
write.sort(key=lambda x: -x["score"])

# ---------- что доработать
signals = defaultdict(list)
pos_files = sorted((_site.CONFIG_DIR / "positions").glob("*.json"))
pos_date = None
if pos_files:
    snap = json.loads(pos_files[-1].read_text())
    pos_date = snap["date"]
    for r in snap["rows"]:
        if not r.get("page") or skip(r["query"]):
            continue
        p = r["position"]
        if p and p <= 10:
            continue
        f = freq(r["query"])
        # Близость к первой странице важнее размера спроса: с 16-го места подняться реально,
        # а «вне первых 50» по широкому запросу это не доработка, а другая задача.
        weight = 0.6 if p and p <= 20 else (0.35 if p and p <= 30 else (0.15 if p else 0.03))
        where = f"на {p}-м месте" if p else f"вне первых {snap.get('depth', 50)}"
        signals[r["page"]].append({"type": "yandex", "query": r["query"], "position": p, "freq": f,
                                   "potential": round(min(f or 0, 20000) * weight),
                                   "text": f"Яндекс: «{r['query']}» {where}" + (f", спрос {num(f)} в месяц" if f else "")})
for r in gsc_rows:
    if 8 <= r["position"] <= 20 and r["impressions"] >= 30 and not skip(r["query"].lower()) and r["page"] not in HUBS:
        signals[r["page"]].append({"type": "google", "query": r["query"], "position": round(r["position"], 1),
                                   "impressions": round(r["impressions"]), "potential": round(r["impressions"] / 3),
                                   "text": f"Google: «{r['query']}» на {r['position']:.0f}-м месте, {round(r['impressions'])} показов за 90 дней"})
if SNAP and (SNAP / "periods.json").exists():
    p28 = json.loads((SNAP / "periods.json").read_text())["periods"].get("28", {})
    for r in p28.get("movers", {}).get("pages", {}).get("down", []):
        if r["key"] not in HUBS:
            signals[r["key"]].append({"type": "drop", "prev": r["prev"], "cur": r["cur"], "potential": r["prev"] - r["cur"],
                                      "text": f"входы из поиска за 28 дней упали: {r['prev']} → {r['cur']}" +
                                              (" (оценка)" if p28.get("prevEstimated") else "")})

improve = []
for page, sig in signals.items():
    sig.sort(key=lambda s: (s["type"] == "yandex" and s["position"] is None, -s["potential"]))
    improve.append({"page": page, "score": sum(s["potential"] for s in sig), "signals": sig[:4],
                    "more": max(0, len(sig) - 4), "why": "; ".join(s["text"] for s in sig[:3])})
improve.sort(key=lambda x: -x["score"])

result = {"asOf": dt.date.today().isoformat(), "write": write[:12], "improve": improve[:12],
          "positionsAsOf": pos_date, "gscError": gsc_error, "spent": spent,
          "wordstatNote": "Спрос из Wordstat это оценка порядка: считаются все запросы со словами фразы, а данные по API могут отставать (см. references/sources.md)."}

# ---------- человеку
print(f"Кандидаты для рекомендаций, {result['asOf']}. Это список с цифрами, а не решение.\n")
if not LINKED["wordstat"]:
    print("Wordstat не подключен: цифр спроса и выдачи нет, список неполный. Подключить: references/onboarding.md\n")
print("ЧТО НАПИСАТЬ (спрос, сила выдачи)")
for i, w in enumerate(write[:12], 1):
    print(f"  {i:2}. {w['topic']}  [оценка {num(w['score'])}; {w['source']}]\n      {w['why']}")
if not write:
    print("  кандидатов нет")
print("\nЧТО ДОРАБОТАТЬ (страницы с самым большим неиспользованным спросом)")
for i, x in enumerate(improve[:12], 1):
    print(f"  {i:2}. {short(x['page'])}  [оценка {num(x['score'])}]")
    for s in x["signals"]:
        print(f"      - {s['text']}")
    if x["more"]:
        print(f"      ... и еще сигналов: {x['more']}")
print(f"\nНовых платных запросов к API за этот запуск: Wordstat {spent['wordstat']}, выдача {spent['serp']}. "
      f"Без цифры спроса осталось фраз: {spent['noFreq']}, без выдачи: {spent['noSerp']}.")
if gsc_error:
    print(f"Search Console: {gsc_error}. Сигналы Google не учтены.")
print(result["wordstatNote"])

if SNAP:
    (SNAP / "opportunities.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
