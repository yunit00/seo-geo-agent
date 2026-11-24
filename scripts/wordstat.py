#!/usr/bin/env python3
"""Спрос и выдача Яндекса: Wordstat и поиск через Yandex Search API.

Отвечает на два вопроса до того, как писать материал: сколько людей это ищет
и кто уже стоит в выдаче.

    wordstat.py check                         проверка ключа и каталога
    wordstat.py top "пластиковые окна цена"   частотность фразы, хвосты и похожие запросы
    wordstat.py top "пластиковые окна" --n 100   больше хвостов
    wordstat.py batch "фраза 1" "фраза 2"     таблица частотности по списку
    wordstat.py batch --file queries.txt      то же из файла, по фразе на строку (# это комментарий)
    wordstat.py serp "как выбрать пластиковые окна"   топ-10 Яндекса: домен, заголовок, адрес

Ключи лежат в secrets.env сайта (как получить: references/onboarding.md):
    YANDEX_SEARCH_API_KEY=...   API-ключ Yandex Cloud для Search API
    YANDEX_FOLDER_ID=...        идентификатор каталога в Yandex Cloud

Запросы платные (тарифы Yandex Search API, порядок цен в references/sources.md),
у Wordstat есть почасовая квота. Поэтому ответы кэшируются на 30 дней в папке
настроек сайта, повторный вопрос денег и квоты не тратит.

Регион по умолчанию берется из site.json (ключ region, 225 это Россия). Цифра Wordstat это число запросов за месяц,
в которых встречаются слова фразы (широкое соответствие), а не точная фраза.
"""
import argparse
import base64
import datetime as dt
import json
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _site  # noqa: E402

import requests  # noqa: E402

BASE = "https://searchapi.api.cloud.yandex.net"
SECRETS = _site.secrets_file()
CACHE = _site.CONFIG_DIR / "wordstat"
DOMAIN = _site.DOMAIN
REGION = str(_site.setting("region", "225"))
CACHE_DAYS = 30
TIMEOUT = 60


def keys():
    data = _site.secrets()
    key, folder = data.get("YANDEX_SEARCH_API_KEY"), data.get("YANDEX_FOLDER_ID")
    if not key or not folder:
        sys.exit("Нет ключа. Впиши YANDEX_SEARCH_API_KEY и YANDEX_FOLDER_ID в "
                 f"{SECRETS}. Как получить: references/onboarding.md, раздел про Wordstat.")
    return key, folder


def post(path: str, body: dict) -> dict:
    key, folder = keys()
    r = requests.post(BASE + path, json={**body, "folderId": folder},
                      headers={"Authorization": f"Api-Key {key}"}, timeout=TIMEOUT)
    if r.status_code in (401, 403):
        sys.exit(f"{r.status_code}: ключ не принят или нет роли search-api.webSearch.user.\n{r.text[:300]}")
    if r.status_code == 429:
        sys.exit("429: квота исчерпана (у Wordstat она почасовая). Подожди и повтори, кэш сохранит уже снятое.")
    if not r.ok:
        sys.exit(f"{r.status_code}: {r.text[:400]}")
    return r.json()


def cache_path(kind: str, phrase: str, region: str) -> Path:
    safe = "".join(c if c.isalnum() else "_" for c in phrase.lower())[:80]
    return CACHE / f"{kind}__{region}__{safe}.json"


def cached(kind: str, phrase: str, region: str, fetch, fresh: bool):
    p = cache_path(kind, phrase, region)
    if not fresh and p.exists():
        saved = json.loads(p.read_text())
        age = (dt.date.today() - dt.date.fromisoformat(saved["date"])).days
        if age <= CACHE_DAYS:
            return saved["data"], saved["date"]
    data = fetch()
    CACHE.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date": dt.date.today().isoformat(), "phrase": phrase,
                             "region": region, "data": data}, ensure_ascii=False))
    return data, dt.date.today().isoformat()


def get_top(phrase: str, n: int, region: str, fresh: bool = False):
    return cached(f"top{n}", phrase, region, lambda: post("/v2/wordstat/topRequests", {
        "phrase": phrase, "numPhrases": n, "regions": [region], "devices": ["DEVICE_ALL"]}), fresh)


def num(x) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def cmd_check(args):
    data, _ = get_top("купить", 1, args.region, fresh=True)
    print(f"Ключ работает. «купить», регион {args.region}: {fmt(num(data.get('totalCount')))} запросов в месяц.")


def cmd_top(args):
    data, day = get_top(args.phrase, args.n, args.region, args.fresh)
    print(f"\n«{args.phrase}», регион {args.region}, снято {day}")
    print(f"Всего запросов в месяц со словами фразы: {fmt(num(data.get('totalCount')))}\n")
    print("Что ищут с этой фразой")
    for row in data.get("results", []):
        print(f"  {fmt(num(row.get('count'))):>12}  {row.get('phrase')}")
    if data.get("associations"):
        print("\nПохожие запросы")
        for row in data["associations"]:
            print(f"  {fmt(num(row.get('count'))):>12}  {row.get('phrase')}")


def cmd_batch(args):
    phrases = list(args.phrases)
    if args.file:
        phrases += [l.strip() for l in Path(args.file).read_text().splitlines()
                    if l.strip() and not l.strip().startswith("#")]
    if not phrases:
        sys.exit("Нет фраз: передай их аргументами или через --file.")
    todo = sum(1 for p in phrases if args.fresh or not cache_path("top1", p, args.region).exists())
    if todo > 95:
        sys.exit(f"Новых фраз {todo}, а квота Wordstat около 100 запросов в час. Раздели список и запускай по частям.")
    rows = []
    for p in phrases:
        hit = cache_path("top1", p, args.region).exists() and not args.fresh
        data, day = get_top(p, 1, args.region, args.fresh)
        rows.append((num(data.get("totalCount")), p, day))
        if not hit:
            time.sleep(0.15)
    rows.sort(reverse=True)
    print(f"\nЧастотность, регион {args.region} (запросов в месяц со словами фразы)\n")
    for count, p, day in rows:
        print(f"  {fmt(count):>12}  {p}   [{day}]")
    print(f"\nфраз: {len(rows)}, из них новых запросов к API: {todo}")


def parse_serp(xml_text: str):
    root = ET.fromstring(xml_text)
    err = root.find(".//error")
    if err is not None:
        sys.exit("Поиск вернул ошибку: " + "".join(err.itertext()).strip())
    out = []
    for doc in root.iter("doc"):
        title = "".join(doc.find("title").itertext()).strip() if doc.find("title") is not None else ""
        out.append({"domain": (doc.findtext("domain") or "").strip(),
                    "url": (doc.findtext("url") or "").strip(), "title": title})
    return out


def cmd_serp(args):
    def fetch():
        j = post("/v2/web/search", {
            "query": {"searchType": "SEARCH_TYPE_RU", "queryText": args.phrase},
            "groupSpec": {"groupsOnPage": args.n}, "region": args.region, "responseFormat": "FORMAT_XML"})
        raw = j.get("rawData")
        if not raw:
            sys.exit("В ответе нет rawData: " + json.dumps(j, ensure_ascii=False)[:300])
        return parse_serp(base64.b64decode(raw).decode("utf-8", "replace"))
    docs, day = cached(f"serp{args.n}", args.phrase, args.region, fetch, args.fresh)
    print(f"\nВыдача Яндекса по «{args.phrase}», регион {args.region}, снято {day}\n")
    for i, d in enumerate(docs, 1):
        mark = "  <-- мы" if DOMAIN in d["domain"] else ""
        print(f"  {i:2}. {d['domain'][:28]:28}  {d['title'][:70]}{mark}")
        print(f"      {d['url'][:110]}")
    ours = [i for i, d in enumerate(docs, 1) if DOMAIN in d["domain"]]
    print(f"\nНаш сайт: {'позиция ' + str(ours[0]) if ours else 'в первых ' + str(len(docs)) + ' нет'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default=REGION, help="код региона Яндекса: 225 Россия, 213 Москва, 2 Санкт-Петербург")
    ap.add_argument("--fresh", action="store_true", help="не брать из кэша")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    t = sub.add_parser("top"); t.add_argument("phrase"); t.add_argument("--n", type=int, default=50); t.set_defaults(fn=cmd_top)
    b = sub.add_parser("batch"); b.add_argument("phrases", nargs="*"); b.add_argument("--file"); b.set_defaults(fn=cmd_batch)
    s = sub.add_parser("serp"); s.add_argument("phrase"); s.add_argument("--n", type=int, default=10); s.set_defaults(fn=cmd_serp)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
