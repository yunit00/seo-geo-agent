#!/usr/bin/env python3
"""IndexNow: сообщить поисковикам о новых и обновленных страницах.

Один запрос на api.indexnow.org расходится по всем участникам протокола:
Яндекс, Bing, Seznam, Naver. Google протокол не поддерживает: для него работает
обычный обход по карте сайта и ручной запрос индексирования в Search Console.

Без пинга новая страница ждет робота днями, с пингом обычно часы.

    indexnow.py init                    придумать ключ и сказать, какой файл положить на сайт
    indexnow.py check                   проверить, что файл ключа отдается с сайта
    indexnow.py /blog/novaya-statya/    отправить конкретные страницы (пути или полные адреса)
    indexnow.py --all                   отправить все адреса из карты сайта
    indexnow.py --dry-run ...           показать список, не отправлять

Ключ IndexNow не секрет: он лежит на сайте открытым файлом <ключ>.txt, так
поисковик убеждается, что запрос шлет владелец домена. Хранится в site.json,
ключ indexnow_key.
"""
import argparse
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _site  # noqa: E402

import requests  # noqa: E402

ENDPOINT = "https://api.indexnow.org/IndexNow"


def key() -> str:
    value = _site.setting("indexnow_key")
    if not value:
        sys.exit("Ключа IndexNow еще нет. Создать: indexnow.py init")
    return value


def cmd_init():
    value = _site.setting("indexnow_key")
    if not value:
        value = secrets.token_hex(16)
        _site.save_setting("indexnow_key", value)
    print(f"Ключ IndexNow: {value}")
    print(f"\nПоложи в корень сайта файл {value}.txt, внутри одна строка: {value}")
    print(f"Он должен открываться по адресу {_site.site_url()}/{value}.txt и не быть закрыт в robots.txt.")
    print("На конструкторе, где файл в корень положить нельзя, IndexNow обычно включается в настройках")
    print("самого конструктора или плагином: тогда этот скрипт не нужен.")
    print("\nПосле выкладки проверь: indexnow.py check")


def cmd_check():
    value = key()
    url = f"{_site.site_url()}/{value}.txt"
    try:
        r = requests.get(url, timeout=20)
    except requests.RequestException as e:
        sys.exit(f"Файл ключа не открылся: {url} ({type(e).__name__})")
    if r.status_code == 200 and r.text.strip() == value:
        print(f"Файл ключа на месте: {url}")
    else:
        sys.exit(f"Файл ключа не найден или в нем не то: {url} отвечает {r.status_code}. "
                 "Содержимое должно совпадать с именем файла без .txt.")


def submit(urls, dry_run: bool) -> int:
    value = key()
    base = _site.site_url()
    host = base.split("://", 1)[-1]
    print(f"IndexNow, адресов: {len(urls)}")
    for u in urls[:50]:
        print(f"  {u}")
    if len(urls) > 50:
        print(f"  ... и еще {len(urls) - 50}")
    if dry_run:
        print("\n--dry-run: ничего не отправлено.")
        return 0
    payload = {"host": host, "key": value, "keyLocation": f"{base}/{value}.txt", "urlList": urls}
    try:
        r = requests.post(ENDPOINT, data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json; charset=utf-8"}, timeout=30)
    except requests.RequestException as e:
        print(f"\nСеть недоступна: {type(e).__name__}")
        return 1
    if r.status_code in (200, 202):     # 202: принято, ключ проверяется асинхронно
        print(f"\nHTTP {r.status_code}: принято. Яндекс и Bing получат список в течение нескольких минут.")
        return 0
    hints = {400: "неверный формат запроса",
             403: "ключ не проходит проверку: файл ключа недоступен по https или еще не выложен",
             422: "адреса не принадлежат заявленному домену, либо ключ не совпадает",
             429: "слишком часто, подожди"}
    print(f"\nHTTP {r.status_code}: {hints.get(r.status_code, 'не принято')}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="*", help="init, check, либо адреса и пути страниц")
    ap.add_argument("--all", action="store_true", help="все адреса из карты сайта")
    ap.add_argument("--dry-run", action="store_true", help="показать список, не отправлять")
    args = ap.parse_args()

    if args.urls == ["init"]:
        cmd_init()
        return 0
    if args.urls == ["check"]:
        cmd_check()
        return 0
    base = _site.site_url()
    if args.all:
        from sitecheck import sitemap_urls
        urls = sitemap_urls(_site.setting("sitemap_url") or f"{base}/sitemap.xml")
    elif args.urls:
        urls = [u if u.startswith("http") else base + (u if u.startswith("/") else "/" + u) for u in args.urls]
    else:
        ap.print_help()
        return 2
    if not urls:
        print("Нечего отправлять.")
        return 0
    return submit(sorted(set(urls)), args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
