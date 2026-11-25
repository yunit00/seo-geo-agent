#!/usr/bin/env python3
"""Данные Google Search Console по сайту через сервисный аккаунт.

Как настроить доступ: references/onboarding.md.

Команды:
    gsc.py check                     проверка доступа; находит ресурс сайта и запоминает его
    gsc.py summary  [--days 28]      клики, показы, CTR, позиция; период к периоду
    gsc.py queries  [--days 28]      запросы с сравнением
    gsc.py pages    [--days 28]      страницы с сравнением
    gsc.py devices  [--days 28]      разбивка по устройствам и странам
    gsc.py sitemaps                  статус карт сайта
    gsc.py inspect <url>             статус индексации конкретного URL
    gsc.py submit   [адрес карты]    переподать карту сайта или RSS-фид (единственная команда, которая пишет)
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _site  # noqa: E402

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    sys.exit("Не хватает библиотек Google. Поставь зависимости: python3 "
             f"{_site.SKILL_DIR / 'scripts/setup.py'} deps")

SCOPES = ["https://www.googleapis.com/auth/webmasters"]

# GSC отдаёт данные с задержкой в пару суток.
LAG_DAYS = 3


def service(_keys=None):
    key_path = _site.gsc_key_file()
    if not key_path.exists():
        sys.exit(f"Нет файла ключа сервисного аккаунта: {key_path}. "
                 "Как получить: references/onboarding.md")
    credentials = service_account.Credentials.from_service_account_file(
        str(key_path), scopes=SCOPES
    )
    return build("searchconsole", "v1", credentials=credentials, cache_discovery=False)


def site_url() -> str:
    """Ресурс Search Console: sc-domain:example.ru или https://example.ru/."""
    value = _site.setting("gsc_site_url")
    if not value:
        sys.exit("Ресурс Search Console не выбран. Запусти gsc.py check: он найдет ресурс сайта сам.")
    return value


def path_of(url: str) -> str:
    """Путь страницы без схемы и домена: https://www.example.ru/a/?x=1 -> /a/?x=1."""
    rest = url.split("://", 1)[-1]
    return "/" + rest.split("/", 1)[1] if "/" in rest else "/"


def find_property(sites: list[dict]) -> str | None:
    """Ресурс нашего домена среди доступных: доменный важнее, чем с префиксом URL."""
    domain = _site.DOMAIN
    try:
        ascii_domain = domain.encode("idna").decode()
    except UnicodeError:
        ascii_domain = domain
    names = {domain, ascii_domain}
    want = [f"sc-domain:{d}" for d in names]
    for d in names:
        want += [f"https://{d}/", f"https://www.{d}/", f"http://{d}/", f"http://www.{d}/"]
    have = {s["siteUrl"] for s in sites if s.get("permissionLevel") != "siteUnverifiedUser"}
    return next((w for w in want if w in have), None)


def periods(days: int) -> tuple[str, str, str, str]:
    end = date.today() - timedelta(days=LAG_DAYS)
    start = end - timedelta(days=days - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    return (str(start), str(end), str(prev_start), str(prev_end))


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
    return f"{'+' if change >= 0 else ''}{change:.0f}%"


def query(api, site: str, start: str, end: str, dimensions=None, limit=100) -> list[dict]:
    body = {"startDate": start, "endDate": end, "rowLimit": limit}
    if dimensions:
        body["dimensions"] = dimensions
    result = api.searchanalytics().query(siteUrl=site, body=body).execute()
    return result.get("rows", [])


def all_rows(days: int, dimensions: list[str], page_url: str | None = None, cap: int = 100_000):
    """Все строки за последние days дней страницами по 25 000, без обрезки хвоста.

    page_url сужает выгрузку до одной страницы. Возвращает (строки, начало, конец);
    окно кончается на сегодня минус LAG_DAYS, потому что данные Google отстают.
    """
    api, site = service(), site_url()
    end = date.today() - timedelta(days=LAG_DAYS)
    start = end - timedelta(days=days - 1)
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(),
            "dimensions": dimensions, "rowLimit": 25_000, "startRow": 0}
    if page_url:
        body["dimensionFilterGroups"] = [{"filters": [
            {"dimension": "page", "operator": "equals", "expression": page_url}]}]
    out = []
    while len(out) < cap:
        chunk = api.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])
        out += chunk
        if len(chunk) < body["rowLimit"]:
            break
        body["startRow"] += body["rowLimit"]
    return out, start, end


def compare(api, site, dimension, d1, d2, p1, p2, limit=25, label="Значение"):
    now = query(api, site, d1, d2, [dimension], limit)
    was = query(api, site, p1, p2, [dimension], 500)
    previous = {r["keys"][0]: r for r in was}

    rows = []
    for row in now:
        key = row["keys"][0]
        old = previous.get(key, {})
        rows.append([
            key[:58],
            int(row["clicks"]),
            delta(row["clicks"], old.get("clicks", 0)),
            int(row["impressions"]),
            f"{row['ctr'] * 100:.1f}%",
            f"{row['position']:.1f}",
        ])
    table(rows, [label, "Клики", "Δ", "Показы", "CTR", "Позиция"])


def cmd_check(args, api, site):
    sites = api.sites().list().execute().get("siteEntry", [])
    print(f"\nДоступно ресурсов: {len(sites)}\n")
    table([[s["siteUrl"], s.get("permissionLevel", "")] for s in sites],
          ["Ресурс", "Права"])
    found = find_property(sites)
    if site and any(s["siteUrl"] == site for s in sites):
        print(f"\nРесурс сайта: {site}")
    elif found:
        _site.save_setting("gsc_site_url", found)
        print(f"\nРесурс сайта найден и сохранён: {found}")
    else:
        print(f"\n⚠ Ресурса для {_site.DOMAIN} в списке нет: сервисный аккаунт не добавлен "
              "в пользователи ресурса или сайт не подтверждён в Search Console.")


def cmd_summary(args, api, site):
    d1, d2, p1, p2 = periods(args.days)
    now = query(api, site, d1, d2)
    was = query(api, site, p1, p2)
    if not now:
        print("  нет данных за период")
        return
    a, b = now[0], (was[0] if was else {"clicks": 0, "impressions": 0, "ctr": 0, "position": 0})

    print(f"\nGoogle Search Console: {d1} — {d2} против {p1} — {p2}\n")
    table([
        ["Клики", int(a["clicks"]), int(b["clicks"]), delta(a["clicks"], b["clicks"])],
        ["Показы", int(a["impressions"]), int(b["impressions"]),
         delta(a["impressions"], b["impressions"])],
        ["CTR", f"{a['ctr'] * 100:.2f}%", f"{b['ctr'] * 100:.2f}%",
         delta(a["ctr"], b["ctr"])],
        ["Позиция", f"{a['position']:.1f}", f"{b['position']:.1f}",
         delta(b["position"], a["position"])],
    ], ["Метрика", "Сейчас", "Было", "Δ"])


def cmd_queries(args, api, site):
    d1, d2, p1, p2 = periods(args.days)
    print(f"\nЗапросы: {d1} — {d2} против предыдущих {args.days} дней\n")
    compare(api, site, "query", d1, d2, p1, p2, limit=40, label="Запрос")


def cmd_pages(args, api, site):
    d1, d2, p1, p2 = periods(args.days)
    print(f"\nСтраницы: {d1} — {d2} против предыдущих {args.days} дней\n")
    compare(api, site, "page", d1, d2, p1, p2, limit=40, label="Страница")


def cmd_devices(args, api, site):
    d1, d2, p1, p2 = periods(args.days)
    print(f"\nУстройства: {d1} — {d2}\n")
    compare(api, site, "device", d1, d2, p1, p2, limit=10, label="Устройство")
    print(f"\nСтраны: {d1} — {d2}\n")
    compare(api, site, "country", d1, d2, p1, p2, limit=10, label="Страна")


def cmd_sitemaps(args, api, site):
    result = api.sitemaps().list(siteUrl=site).execute().get("sitemap", [])
    print(f"\nКарт сайта: {len(result)}\n")
    rows = []
    for item in result:
        contents = item.get("contents", [{}])[0]
        rows.append([
            item["path"],
            item.get("lastSubmitted", "")[:10],
            item.get("lastDownloaded", "")[:10],
            contents.get("submitted", "—"),
            # GSC отдаёт счётчики строками, поэтому "0" без int() читается как истина.
            int(item.get("errors") or 0),
            int(item.get("warnings") or 0),
        ])
    table(rows, ["Карта", "Подана", "Скачана", "URL", "Ошибки", "Предупр."])


def cmd_inspect(args, api, site):
    if not args.url:
        sys.exit(f"Укажи URL: gsc.py inspect {_site.site_url()}/страница/")
    result = api.urlInspection().index().inspect(body={
        "inspectionUrl": args.url,
        "siteUrl": site,
    }).execute()
    status = result.get("inspectionResult", {}).get("indexStatusResult", {})
    print(f"\n{args.url}\n")
    table([
        ["Вердикт", status.get("verdict", "—")],
        ["В индексе", status.get("coverageState", "—")],
        ["Робот", status.get("robotsTxtState", "—")],
        ["Индексация", status.get("indexingState", "—")],
        ["Последний обход", (status.get("lastCrawlTime") or "—")[:10]],
        ["Канонический (Google)", status.get("googleCanonical", "—")],
        ["Канонический (наш)", status.get("userCanonical", "—")],
        ["Sitemap", ", ".join(status.get("sitemap", [])) or "нет"],
    ], ["Показатель", "Значение"])


def cmd_submit(args, api, site):
    """Переподача карты сайта. Google перечитывает её не по расписанию,
    а по факту подачи, поэтому после публикации это самый дешёвый способ
    ускорить обход. Тот же вызов принимает RSS-фид: для Google это
    такая же карта, только короткая и всегда свежая."""
    feed = args.url or _site.setting("sitemap_url") or f"{_site.site_url()}/sitemap.xml"
    api.sitemaps().submit(siteUrl=site, feedpath=feed).execute()
    print(f"  подано: {feed}")


COMMANDS = {
    "check": cmd_check,
    "summary": cmd_summary,
    "queries": cmd_queries,
    "pages": cmd_pages,
    "devices": cmd_devices,
    "sitemaps": cmd_sitemaps,
    "inspect": cmd_inspect,
    "submit": cmd_submit,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("url", nargs="?")
    parser.add_argument("--days", type=int, default=28)
    args = parser.parse_args()

    site = _site.setting("gsc_site_url") if args.command == "check" else site_url()
    try:
        COMMANDS[args.command](args, service(), site)
    except HttpError as error:
        text = str(error)
        if "has not been used in project" in text or "accessNotConfigured" in text:
            sys.exit("Search Console API не включён в проекте Google Cloud.\n"
                     "Включить: https://console.cloud.google.com/apis/library/"
                     "searchconsole.googleapis.com")
        if error.resp.status == 403:
            sys.exit("403: сервисный аккаунт не добавлен в пользователи ресурса.\n"
                     "Настройки ресурса в Search Console → Пользователи и разрешения → "
                     "добавить адрес из client_email с полным доступом.")
        sys.exit(f"Ошибка Google API {error.resp.status}: {error._get_reason()}")
    print()


if __name__ == "__main__":
    main()
