#!/usr/bin/env python3
"""Проверка индексации страниц сайта в Google и Яндексе.

Берет список адресов из карты сайта и сверяет его с двумя источниками:
Google Search Console (URL Inspection API) и Яндекс.Вебмастер (страницы в
поиске). Показывает, что уже в индексе, а что поисковик еще не забрал.

Зачем: после публикации важно знать не «отправили ли пинг», а «дошло ли до
выдачи». IndexNow ускоряет Яндекс и Bing, Google живет своим обходом, и
отставание там видно только такой сверкой.

    indexation.py                     все адреса из карты сайта (не больше --limit)
    indexation.py blog                только раздел /blog/
    indexation.py --missing           показать только то, чего нет в индексе
    indexation.py --google            спросить один Google
    indexation.py --yandex            спросить один Яндекс
    indexation.py --limit 500         сколько адресов проверять в Google (по умолчанию 300)
    indexation.py --json              сырые данные вместо таблицы

Квота URL Inspection API: 2000 запросов в сутки на ресурс, запрос на страницу.
Источник, который не подключен, пропускается с пометкой.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _site  # noqa: E402

import requests  # noqa: E402

from sitecheck import sitemap_urls  # noqa: E402

WEBMASTER = "https://api.webmaster.yandex.net/v4"
GOOGLE_OK = "PASS"      # вердикт PASS бывает только у страниц, реально попавших в индекс


def table(rows, headers):
    if not rows:
        print("  нет данных")
        return
    cells = [[str(c) for c in row] for row in rows]
    widths = [max(len(headers[i]), max(len(row[i]) for row in cells)) for i in range(len(headers))]
    print("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  " + "  ".join("-" * w for w in widths))
    for row in cells:
        print("  " + "  ".join(row[i].ljust(widths[i]) for i in range(len(row))))


def norm(url: str) -> str:
    rest = url.split("://", 1)[-1].split("?")[0].split("#")[0]
    return rest.removeprefix("www.").rstrip("/") + "/"


def google_status(urls):
    """Вердикт Google по каждому адресу. Один запрос на страницу, отсюда пауза."""
    import gsc
    api, site = gsc.service(), gsc.site_url()
    out = {}
    for url in urls:
        try:
            answer = api.urlInspection().index().inspect(body={"inspectionUrl": url, "siteUrl": site}).execute()
            status = answer.get("inspectionResult", {}).get("indexStatusResult", {})
            out[url] = {"verdict": status.get("verdict", "—"), "state": status.get("coverageState", "—"),
                        "crawl": (status.get("lastCrawlTime") or "—")[:10],
                        "canonical": status.get("googleCanonical", "")}
        except gsc.HttpError as error:
            if error.resp.status == 403:
                sys.exit("403: сервисный аккаунт не добавлен в пользователи ресурса в Search Console.")
            if error.resp.status == 429:
                print("  429: кончилась суточная квота URL Inspection, остальные адреса не проверены")
                break
            out[url] = {"verdict": "ОШИБКА", "state": error._get_reason(), "crawl": "—", "canonical": ""}
        time.sleep(0.3)
    return out


def yandex_in_search() -> set[str]:
    """Страницы, которые Вебмастер считает находящимися в поиске (это выборка, до 50 тысяч)."""
    token = _site.secret("YANDEX_OAUTH_TOKEN")
    host = _site.setting("webmaster_host_id")
    headers = {"Authorization": f"OAuth {token}"}
    answer = requests.get(f"{WEBMASTER}/user", headers=headers, timeout=30)
    if answer.status_code != 200:
        sys.exit(f"Вебмастер {answer.status_code}: {answer.text[:200]}")
    user_id = answer.json()["user_id"]
    found, offset = set(), 0
    while True:
        page = requests.get(f"{WEBMASTER}/user/{user_id}/hosts/{host}/search-urls/in-search/samples",
                            headers=headers, params={"limit": 100, "offset": offset}, timeout=30)
        if page.status_code != 200:
            sys.exit(f"Вебмастер {page.status_code}: {page.text[:200]}")
        samples = page.json().get("samples", [])
        found.update(norm(s["url"]) for s in samples)
        if len(samples) < 100:
            return found
        offset += 100


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("prefix", nargs="?", help="раздел сайта, например blog")
    parser.add_argument("--missing", action="store_true", help="только не в индексе")
    parser.add_argument("--google", action="store_true", help="только Google")
    parser.add_argument("--yandex", action="store_true", help="только Яндекс")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    linked = _site.connected()
    ask_google = (args.google or not args.yandex) and linked["gsc"]
    ask_yandex = (args.yandex or not args.google) and linked["webmaster"]
    skipped = [name for name, want, have in (("Google", args.google or not args.yandex, linked["gsc"]),
                                             ("Яндекс", args.yandex or not args.google, linked["webmaster"]))
               if want and not have]
    if not ask_google and not ask_yandex:
        sys.exit("Ни Search Console, ни Вебмастер не подключены: сверять не с чем.")

    base = _site.site_url()
    sitemap = _site.setting("sitemap_url") or f"{base}/sitemap.xml"
    urls = list(dict.fromkeys(sitemap_urls(sitemap)))
    if args.prefix:
        want = "/" + args.prefix.strip("/") + "/"
        urls = [u for u in urls if ("/" + u.split("://", 1)[-1].split("/", 1)[-1]).startswith(want)]
    if not urls:
        sys.exit(f"В карте сайта {sitemap} по этому условию ничего нет. Если карта лежит по другому "
                 "адресу, впиши его в site.json, ключ sitemap_url.")
    total = len(urls)
    cut = ask_google and total > args.limit
    if cut:
        urls = urls[:args.limit]
    if not args.json:
        print(f"\nАдресов в карте сайта: {total}" + (f" (фильтр {args.prefix})" if args.prefix else "")
              + (f"; проверяю первые {args.limit}, остальные через --limit" if cut else ""))
        for name in skipped:
            print(f"{name}: источник не подключен, не проверяется")
        print()

    google = google_status(urls) if ask_google else {}
    yandex = yandex_in_search() if ask_yandex else set()

    rows, missing_g, missing_y = [], [], []
    for url in urls:
        path = "/" + url.split("://", 1)[-1].split("/", 1)[-1] if "/" in url.split("://", 1)[-1] else "/"
        g = google.get(url, {})
        in_google = g.get("verdict") == GOOGLE_OK
        in_yandex = norm(url) in yandex
        if ask_google and url in google and not in_google:
            missing_g.append((path, g.get("state", "—")))
        if ask_yandex and not in_yandex:
            missing_y.append(path)
        if args.missing and (not ask_google or in_google) and (not ask_yandex or in_yandex):
            continue
        row = [path[:70]]
        if ask_google:
            row += ["да" if in_google else ("нет" if url in google else "?"), g.get("crawl", "—")]
        if ask_yandex:
            row += ["да" if in_yandex else "нет"]
        rows.append(row)

    if args.json:
        print(json.dumps({"urls": total, "checked": len(urls), "google": google,
                          "yandex_missing": missing_y, "not_connected": skipped}, ensure_ascii=False, indent=2))
        return

    headers = ["Страница"] + (["Google", "Обход"] if ask_google else []) + (["Яндекс"] if ask_yandex else [])
    table(rows, headers)
    if ask_google:
        print(f"\nGoogle: в индексе {len(google) - len(missing_g)} из {len(google)} проверенных")
        for path, state in missing_g:
            print(f"  нет: {path}: {state}")
    if ask_yandex:
        print(f"\nЯндекс: в поиске {len(urls) - len(missing_y)} из {len(urls)}")
        for path in missing_y:
            print(f"  нет: {path}")
    if missing_g or missing_y:
        print("\nЧто с этим делать: свежие страницы пингуются через indexnow.py (Яндекс и Bing), для Google "
              "остается запрос на индексирование вручную в Search Console. Статус «Crawled, not indexed» "
              "пингами не лечится: это вопрос к содержанию страницы.")


if __name__ == "__main__":
    main()
