#!/usr/bin/env python3
"""Данные Яндекс.Метрики и Яндекс.Вебмастера по сайту.

Один OAuth-токен работает на оба сервиса. Как его получить: references/onboarding.md.

Команды:
    yandex.py check                        проверка токена; находит счетчик и сайт и запоминает их
    yandex.py summary   [--days 30]        визиты, посетители, отказы; период к периоду
    yandex.py sources   [--days 30]        источники трафика, рефереры, UTM
    yandex.py pages     [--days 30]        страницы входа
    yandex.py search    [--days 30]        поисковые фразы из Метрики
    yandex.py goals                        список целей в счетчике
    yandex.py conversions [--days 30]      достижения целей по источникам и страницам входа
    yandex.py queries                      запросы из Вебмастера (показы, клики, позиция)
    yandex.py history   [--days 60]        показы и клики из поиска Яндекса по дням, JSON
    yandex.py indexing                     страницы в поиске, ИКС, проблемы сайта

Ничего не меняет ни на сайте, ни в сервисах: только читает.
"""

import argparse
import json
import sys
from datetime import date, timedelta

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _site  # noqa: E402

import requests  # noqa: E402

METRIKA_STAT = "https://api-metrika.yandex.net/stat/v1/data"
METRIKA_MGMT = "https://api-metrika.yandex.net/management/v1"
WEBMASTER = "https://api.webmaster.yandex.net/v4"

TIMEOUT = 60


def api(url: str, token: str, params: dict | None = None) -> dict:
    response = requests.get(
        url,
        params=params or {},
        headers={"Authorization": f"OAuth {token}"},
        timeout=TIMEOUT,
    )
    if response.status_code == 401:
        sys.exit("401: токен недействителен или протух. Выпусти новый: references/onboarding.md")
    if response.status_code == 403:
        sys.exit(f"403: у токена нет нужного доступа.\n{response.text[:400]}")
    if not response.ok:
        sys.exit(f"{response.status_code}: {response.text[:600]}")
    return response.json()


# ---------------------------------------------------------------- вывод

def table(rows: list[list], headers: list[str]) -> None:
    if not rows:
        print("  нет данных")
        return
    cells = [[str(c) for c in row] for row in rows]
    widths = [
        max(len(headers[i]), max(len(row[i]) for row in cells))
        for i in range(len(headers))
    ]
    print("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  " + "  ".join("-" * w for w in widths))
    for row in cells:
        print("  " + "  ".join(row[i].ljust(widths[i]) for i in range(len(row))))


def delta(now: float, was: float) -> str:
    if was == 0:
        return "—" if now == 0 else "новое"
    change = (now - was) / was * 100
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:.0f}%"


def periods(days: int) -> tuple[str, str, str, str]:
    """Текущий период и такой же по длине непосредственно перед ним."""
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    return (str(start), str(end), str(prev_start), str(prev_end))


# ---------------------------------------------------------------- Метрика

def metrika(token: str, counter: str, **params) -> dict:
    base = {"ids": counter, "accuracy": "full", "limit": 100}
    base.update(params)
    return api(METRIKA_STAT, token, base)


def totals(token: str, counter: str, metrics: str, d1: str, d2: str) -> list[float]:
    data = metrika(token, counter, metrics=metrics, date1=d1, date2=d2)
    return data.get("totals", [])


def cmd_summary(args, keys):
    token, counter = keys["YANDEX_OAUTH_TOKEN"], counter_id()
    d1, d2, p1, p2 = periods(args.days)
    metrics = (
        "ym:s:visits,ym:s:users,ym:s:pageviews,"
        "ym:s:bounceRate,ym:s:avgVisitDurationSeconds,ym:s:percentNewVisitors"
    )
    names = ["Визиты", "Посетители", "Просмотры", "Отказы %", "Время, с", "Новые %"]

    now = totals(token, counter, metrics, d1, d2)
    was = totals(token, counter, metrics, p1, p2)

    print(f"\nМетрика: {d1} — {d2} против {p1} — {p2}\n")
    rows = [
        [names[i], f"{now[i]:.1f}".rstrip("0").rstrip("."),
         f"{was[i]:.1f}".rstrip("0").rstrip("."), delta(now[i], was[i])]
        for i in range(len(names))
    ]
    table(rows, ["Метрика", "Сейчас", "Было", "Δ"])


def dimension_report(token, counter, dimension, d1, d2, p1, p2, limit=15):
    """Одно измерение, визиты, с сравнением по тому же ключу."""
    now = metrika(token, counter, metrics="ym:s:visits", dimensions=dimension,
                  date1=d1, date2=d2, sort="-ym:s:visits", limit=limit)
    was = metrika(token, counter, metrics="ym:s:visits", dimensions=dimension,
                  date1=p1, date2=p2, sort="-ym:s:visits", limit=200)

    previous = {}
    for row in was.get("data", []):
        key = row["dimensions"][0].get("name") or "—"
        previous[key] = row["metrics"][0]

    rows = []
    for row in now.get("data", []):
        key = row["dimensions"][0].get("name") or "—"
        visits = row["metrics"][0]
        rows.append([key[:60], int(visits), int(previous.get(key, 0)),
                     delta(visits, previous.get(key, 0))])
    return rows


def cmd_sources(args, keys):
    token, counter = keys["YANDEX_OAUTH_TOKEN"], counter_id()
    d1, d2, p1, p2 = periods(args.days)
    print(f"\nИсточники трафика: {d1} — {d2} против предыдущих {args.days} дней")

    blocks = [
        ("Тип источника", "ym:s:lastsignTrafficSource"),
        ("Поисковая система", "ym:s:lastsignSearchEngine"),
        ("Сайт-реферер", "ym:s:lastsignReferalSource"),
        ("UTM source", "ym:s:UTMSource"),
        ("UTM campaign", "ym:s:UTMCampaign"),
    ]
    for title, dimension in blocks:
        print(f"\n{title}")
        try:
            rows = dimension_report(token, counter, dimension, d1, d2, p1, p2)
            table(rows, ["Значение", "Визиты", "Было", "Δ"])
        except SystemExit as error:
            print(f"  недоступно: {error}")


def cmd_pages(args, keys):
    token, counter = keys["YANDEX_OAUTH_TOKEN"], counter_id()
    d1, d2, p1, p2 = periods(args.days)
    print(f"\nСтраницы входа: {d1} — {d2} против предыдущих {args.days} дней\n")
    rows = dimension_report(token, counter, "ym:s:startURLPath", d1, d2, p1, p2, limit=30)
    table(rows, ["Страница", "Визиты", "Было", "Δ"])


def cmd_search(args, keys):
    token, counter = keys["YANDEX_OAUTH_TOKEN"], counter_id()
    d1, d2, p1, p2 = periods(args.days)
    print(f"\nПоисковые фразы (по данным Метрики): {d1} — {d2}\n")
    rows = dimension_report(token, counter, "ym:s:searchPhrase", d1, d2, p1, p2, limit=30)
    table(rows, ["Фраза", "Визиты", "Было", "Δ"])


def cmd_conversions(args, keys):
    """Достижения целей: всего, по источникам и по страницам входа."""
    token, counter = keys["YANDEX_OAUTH_TOKEN"], counter_id()
    d1, d2, p1, p2 = periods(args.days)

    goals = api(f"{METRIKA_MGMT}/counter/{counter}/goals", token).get("goals", [])
    if not goals:
        print("  Целей в счётчике нет: конверсии не измеряются. Как завести: references/setup-checklist.md")
        return

    metrics = ",".join(f"ym:s:goal{g['id']}reaches" for g in goals)
    now = totals(token, counter, metrics, d1, d2)
    was = totals(token, counter, metrics, p1, p2)

    print(f"\nДостижения целей: {d1} — {d2} против предыдущих {args.days} дней\n")
    table([[goals[i]["name"], int(now[i]), int(was[i]), delta(now[i], was[i])]
           for i in range(len(goals))], ["Цель", "Сейчас", "Было", "Δ"])

    for goal in goals:
        metric = f"ym:s:goal{goal['id']}reaches"
        if not totals(token, counter, metric, d1, d2)[0]:
            continue
        for title, dimension in [("источникам", "ym:s:lastsignTrafficSource"),
                                 ("страницам входа", "ym:s:startURLPath")]:
            data = metrika(token, counter, metrics=f"ym:s:visits,{metric}",
                           dimensions=dimension, date1=d1, date2=d2,
                           sort=f"-{metric}", limit=15)
            rows = [[r["dimensions"][0].get("name") or "—", int(r["metrics"][1]),
                     int(r["metrics"][0]),
                     f"{r['metrics'][1] / r['metrics'][0] * 100:.1f}%" if r["metrics"][0] else "—"]
                    for r in data.get("data", []) if r["metrics"][1]]
            if rows:
                print(f"\n«{goal['name']}» по {title}\n")
                table(rows, ["Значение", "Достижений", "Визитов", "Конверсия"])


def cmd_goals(args, keys):
    token, counter = keys["YANDEX_OAUTH_TOKEN"], counter_id()
    data = api(f"{METRIKA_MGMT}/counter/{counter}/goals", token)
    goals = data.get("goals", [])
    print(f"\nЦелей в счётчике {counter}: {len(goals)}\n")
    if not goals:
        print("  Целей нет — конверсии не измеряются.")
        return
    table([[g["id"], g["name"], g.get("type", ""), g.get("is_retargeting", 0)]
           for g in goals], ["ID", "Название", "Тип", "Ретаргетинг"])


# ---------------------------------------------------------------- Вебмастер

def host_matches(host: dict, domain: str) -> bool:
    """Хост Вебмастера относится к нашему домену (с www и без, http и https)."""
    name = host.get("ascii_host_url", "").split("://")[-1].strip("/").removeprefix("www.")
    try:
        ascii_domain = domain.encode("idna").decode()
    except UnicodeError:
        ascii_domain = domain
    return name in (domain, ascii_domain)


def pick_host(hosts: list[dict], domain: str) -> dict | None:
    """Из нескольких зеркал берем подтвержденное, при равенстве https."""
    ours = [h for h in hosts if host_matches(h, domain)]
    ours.sort(key=lambda h: (not h.get("verified"), not h.get("ascii_host_url", "").startswith("https")))
    return ours[0] if ours else None


def webmaster_ids(token: str) -> tuple[str, str]:
    user_id = api(f"{WEBMASTER}/user/", token)["user_id"]
    host_id = _site.setting("webmaster_host_id")
    if not host_id:
        hosts = api(f"{WEBMASTER}/user/{user_id}/hosts/", token)["hosts"]
        host = pick_host(hosts, _site.DOMAIN)
        if not host:
            sys.exit(f"В Вебмастере под этим аккаунтом нет сайта {_site.DOMAIN}. "
                     "Добавь и подтверди сайт: references/onboarding.md")
        host_id = host["host_id"]
        _site.save_setting("webmaster_host_id", host_id)
    return user_id, host_id


def counter_id() -> str:
    value = _site.setting("metrika_counter")
    if not value:
        sys.exit("Счетчик Метрики не выбран. Запусти yandex.py check: он найдет счетчик сайта сам.")
    return str(value)


def api_post(url: str, token: str, body: dict) -> dict:
    response = requests.post(
        url,
        json=body,
        headers={"Authorization": f"OAuth {token}"},
        timeout=TIMEOUT,
    )
    if response.status_code == 401:
        sys.exit("401: токен недействителен или протух. Выпусти новый: references/onboarding.md")
    if response.status_code == 403:
        sys.exit(f"403: у токена нет нужного доступа.\n{response.text[:400]}")
    if not response.ok:
        sys.exit(f"{response.status_code}: {response.text[:600]}")
    return response.json()


def query_analytics(token: str, user_id: str, host_id: str, by: str = "QUERY", max_rows: int = 500) -> dict:
    """Запросы (или страницы, by="URL") из отчёта «Управление запросами».

    Старый метод search-queries/popular с 14.10.2026 отдаёт пустоту: Яндекс перевёл
    статистику запросов на query-analytics. Новый метод не берёт период, он всегда
    отдаёт последние 14 дней, по которым у Яндекса есть данные (отстаёт на 3–4 дня).
    Строки приходят по убыванию показов, поэтому берём верхние max_rows: весь список
    это 20+ тысяч запросов и две с половиной минуты. Фильтр statistic_filters не
    использовать: с ним Яндекс молча теряет большую часть запросов (проверено 24.11.2026).
    Дни складываем сами: показы и клики суммой, позицию средней с весом показов.
    Возвращает {"from", "to", "count", "rows": {текст: {"shows", "clicks", "pos"}}}.
    """
    url = f"{WEBMASTER}/user/{user_id}/hosts/{host_id}/query-analytics/list"
    rows, dates, offset, count = {}, set(), 0, 0
    while offset < max_rows:
        data = api_post(url, token, {"offset": offset, "limit": min(500, max_rows - offset),
                                     "device_type_indicator": "ALL", "text_indicator": by})
        count = data.get("count", 0)
        items = data.get("text_indicator_to_statistics", [])
        for item in items:
            days = {}
            for st in item.get("statistics", []):
                dates.add(st["date"])
                days.setdefault(st["date"], {})[st["field"]] = st["value"] or 0
            shows = sum(d.get("IMPRESSIONS", 0) for d in days.values())
            clicks = sum(d.get("CLICKS", 0) for d in days.values())
            weighted = sum(d.get("IMPRESSIONS", 0) * d.get("POSITION", 0) for d in days.values() if d.get("POSITION"))
            rows[item["text_indicator"]["value"]] = {
                "shows": int(shows), "clicks": int(clicks),
                "pos": round(weighted / shows, 1) if shows and weighted else None,
            }
        offset += len(items)
        if not items or offset >= count:
            break
    ordered = sorted(dates)
    return {"from": ordered[0] if ordered else None, "to": ordered[-1] if ordered else None, "count": count,
            "rows": dict(sorted(rows.items(), key=lambda kv: -kv[1]["shows"]))}


def cmd_queries(args, keys):
    token = keys["YANDEX_OAUTH_TOKEN"]
    user_id, host_id = webmaster_ids(token)
    data = query_analytics(token, user_id, host_id)
    pages = query_analytics(token, user_id, host_id, by="URL")   # страниц мало, итоги по ним полные

    print(f"\nЗапросы в Яндексе: {data['from']} — {data['to']}"
          f" (Вебмастер отдаёт только последние 14 дней, --days не влияет)\n")
    total_shows = sum(r["shows"] for r in pages["rows"].values())
    total_clicks = sum(r["clicks"] for r in pages["rows"].values())
    print(f"  Всего: запросов {data['count']}, показов {total_shows}, кликов {total_clicks}\n")
    rows = []
    for text, r in list(data["rows"].items())[:50]:
        ctr = f"{r['clicks'] / r['shows'] * 100:.2f}%" if r["shows"] else "—"
        rows.append([text[:55], r["shows"], r["clicks"], ctr, f"{r['pos']:.1f}" if r["pos"] else "—"])
    table(rows, ["Запрос", "Показы", "Клики", "CTR", "Позиция"])

    print("\nСтраницы в Яндексе\n")
    table([[u[:60], r["shows"], r["clicks"], f"{r['pos']:.1f}" if r["pos"] else "—"]
           for u, r in list(pages["rows"].items())[:15]], ["Страница", "Показы", "Клики", "Позиция"])


def cmd_history(args, keys):
    """Показы и клики из поиска Яндекса по дням, JSON. Для сравнения периодов.

    Последние дни Яндекс отдаёт нулями, пока не посчитал (отставание 3–4 дня):
    хвостовые нули отрезаем, lastDay это последний день с данными.
    """
    token = keys["YANDEX_OAUTH_TOKEN"]
    user_id, host_id = webmaster_ids(token)
    end = date.today()
    start = end - timedelta(days=max(args.days, 30))
    data = api(f"{WEBMASTER}/user/{user_id}/hosts/{host_id}/search-queries/all/history/", token, [
        ("query_indicator", "TOTAL_SHOWS"), ("query_indicator", "TOTAL_CLICKS"),
        ("date_from", str(start)), ("date_to", str(end)),
    ])
    ind = data.get("indicators", {})
    clicks = {x["date"][:10]: int(x["value"] or 0) for x in ind.get("TOTAL_CLICKS", [])}
    days = [{"date": x["date"][:10], "shows": int(x["value"] or 0), "clicks": clicks.get(x["date"][:10], 0)}
            for x in ind.get("TOTAL_SHOWS", [])]
    while days and not days[-1]["shows"]:
        days.pop()
    print(json.dumps({"lastDay": days[-1]["date"] if days else None, "days": days}, ensure_ascii=False))


def cmd_indexing(args, keys):
    token = keys["YANDEX_OAUTH_TOKEN"]
    user_id, host_id = webmaster_ids(token)
    base = f"{WEBMASTER}/user/{user_id}/hosts/{host_id}"

    summary = api(f"{base}/summary/", token)
    print("\nСводка по хосту\n")
    table([
        ["ИКС", summary.get("sqi", "—")],
        ["Страниц в поиске", summary.get("searchable_pages_count", "—")],
        ["Исключено", summary.get("excluded_pages_count", "—")],
        ["Критичных проблем", summary.get("site_problems", {}).get("CRITICAL", 0)],
        ["Возможных проблем", summary.get("site_problems", {}).get("POSSIBLE_PROBLEM", 0)],
        ["Рекомендаций", summary.get("site_problems", {}).get("RECOMMENDATION", 0)],
    ], ["Показатель", "Значение"])

    history = api(f"{base}/search-urls/in-search/history/", token)
    points = history.get("history", [])[-10:]
    if points:
        print("\nСтраниц в поиске, динамика\n")
        table([[p["date"][:10], p["value"]] for p in points], ["Дата", "Страниц"])


# ---------------------------------------------------------------- check

def cmd_check(args, keys):
    token = keys["YANDEX_OAUTH_TOKEN"]
    domain = _site.DOMAIN
    print("\nПроверяю токен...\n")

    counters = api(f"{METRIKA_MGMT}/counters", token).get("counters", [])
    print(f"Метрика: доступно счётчиков: {len(counters)}")
    table([[c["id"], c["name"], c.get("site", "")] for c in counters],
          ["ID", "Название", "Сайт"])
    saved = _site.setting("metrika_counter")
    ours = [c for c in counters if (c.get("site") or "").removeprefix("www.") == domain]
    if saved:
        if not any(str(c["id"]) == str(saved) for c in counters):
            print(f"\n⚠ В настройках счётчик {saved}, но этому токену он недоступен.")
        else:
            print(f"\nСчётчик сайта: {saved}")
    elif len(ours) == 1:
        _site.save_setting("metrika_counter", str(ours[0]["id"]))
        print(f"\nСчётчик сайта найден и сохранён: {ours[0]['id']}")
    elif ours:
        print(f"\nНа {domain} несколько счётчиков: {', '.join(str(c['id']) for c in ours)}. "
              "Спроси владельца, какой основной, и впиши его в site.json, ключ metrika_counter.")
    else:
        print(f"\n⚠ Счётчика для {domain} под этим аккаунтом нет. Либо счётчик не установлен, "
              "либо он живёт в другом аккаунте Яндекса: references/onboarding.md")

    try:
        user_id = api(f"{WEBMASTER}/user/", token)["user_id"]
        hosts = api(f"{WEBMASTER}/user/{user_id}/hosts/", token)["hosts"]
        print(f"\nВебмастер: хостов {len(hosts)}")
        table([[h["ascii_host_url"], h.get("verified", "")] for h in hosts],
              ["Хост", "Подтверждён"])
        host = pick_host(hosts, domain)
        if host:
            _site.save_setting("webmaster_host_id", host["host_id"])
            print(f"\nСайт в Вебмастере найден и сохранён: {host['ascii_host_url']}"
                  + ("" if host.get("verified") else "  ⚠ права на сайт не подтверждены"))
        else:
            print(f"\n⚠ Сайта {domain} в Вебмастере под этим аккаунтом нет: добавь и подтверди его.")
    except SystemExit as error:
        print(f"\nВебмастер недоступен: {error}")
        print("Скорее всего, в OAuth-приложении отмечены не все доступы Вебмастера.")


COMMANDS = {
    "check": cmd_check,
    "summary": cmd_summary,
    "sources": cmd_sources,
    "pages": cmd_pages,
    "search": cmd_search,
    "goals": cmd_goals,
    "conversions": cmd_conversions,
    "queries": cmd_queries,
    "history": cmd_history,
    "indexing": cmd_indexing,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    keys = _site.secrets()
    if not keys.get("YANDEX_OAUTH_TOKEN"):
        _site.secret("YANDEX_OAUTH_TOKEN")
    COMMANDS[args.command](args, keys)
    print()


if __name__ == "__main__":
    main()
