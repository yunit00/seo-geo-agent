#!/usr/bin/env python3
"""Слова запросов в заголовках страницы: какая доля ее показов в Google приходится
на запросы, все слова которых стоят в title, H1, H2 или H3.

    coverage.py --page /blog/statya/ [--html черновик.html] [--days 90] [--top 20]

Запускать до правки и после нее, на черновике. Если доля упала, правка выкинула
слова, по которым страницу уже находят, и их надо вернуть в title или подзаголовок.
С --html печатаются обе доли и список запросов, которые черновик потерял.

Текущая версия страницы берется из папки исходников (`site_dir` в site.json),
если там есть файл, иначе с живого сайта.

Слова сравниваются по началу, окончания русских слов отбрасываются грубо
(«цены» и «цена» совпадут). Перевод и транслит не распознаются: «iphone» и
«айфон» для скрипта разные слова, такие пары смотреть глазами. Служебные слова
не считаются. Только Google: Вебмастер не отдает запросы в разбивке по странице.
"""
import argparse
import html as htmlmod
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _site  # noqa: E402

STOP = set("""как что это для или и в во на с со по к ко о об от до из у за при без над под про
чем где когда кто такой такое такая какой какая какие каких зачем почему ли же бы не ни а но
the a an of to for in on with how what is are and or vs""".split())

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--page", required=True, help="путь страницы, например /blog/statya/")
ap.add_argument("--html", help="локальный файл черновика")
ap.add_argument("--days", type=int, default=90)
ap.add_argument("--top", type=int, default=20)
args = ap.parse_args()

import gsc  # noqa: E402
import _pages  # noqa: E402


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-zа-я0-9]+", text.lower().replace("ё", "е"))
            if len(w) >= 2 and w not in STOP]


def stem(w: str) -> str:
    return w if len(w) <= 4 or w.isdigit() else w[:max(4, len(w) - 2)]


def heading_stems(doc: str) -> tuple[set, list[str]]:
    parts = re.findall(r"<title[^>]*>(.*?)</title>", doc, re.S | re.I)
    parts += re.findall(r"<h[1-3][^>]*>(.*?)</h[1-3]>", doc, re.S | re.I)
    clean = [re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", p))).strip() for p in parts]
    return {stem(w) for p in clean for w in words(p)}, clean


def missing(query: str, stems: set) -> list[str]:
    out = []
    for w in words(query):
        s = stem(w)
        if not any(h.startswith(s) or s.startswith(h) for h in stems if len(h) >= 3 or h == s):
            out.append(w)
    return out


def share(label: str, stems: set, data: list, total: int) -> set:
    ok = [d for d in data if not missing(d[0], stems)]
    n = sum(d[1] for d in ok)
    print(f"{label}: покрыто {n} из {total} показов ({n / total:.0%})")
    return {d[0] for d in ok}


url = _site.site_url() + args.page
noise = [re.compile(p, re.I) for p in _site.SITE.get("noise_queries", [])]
rows, start, end = gsc.all_rows(args.days, ["query"], page_url=url)
data = sorted(((r["keys"][0], r["impressions"], r["clicks"], r["position"]) for r in rows
               if not any(x.search(r["keys"][0]) for x in noise)), key=lambda d: -d[1])
total = sum(d[1] for d in data)
print(f"{url}\nGoogle, {start} .. {end}: запросов {len(data)}, показов {total}\n")
if not data:
    sys.exit("Показов в Google у страницы нет: сравнивать не с чем.")

where, doc = _pages.page_html(args.page)
live_stems, live_heads = heading_stems(doc)
live_ok = share(f"Сейчас ({where})", live_stems, data, total)
stems = live_stems
if args.html:
    draft_stems, _ = heading_stems(Path(args.html).read_text(encoding="utf-8"))
    draft_ok = share("Черновик", draft_stems, data, total)
    lost = [d for d in data if d[0] in live_ok and d[0] not in draft_ok]
    if lost:
        print("\nЧерновик потерял слова этих запросов (вернуть в title или подзаголовок):")
        for q, im, cl, pos in lost[:args.top]:
            print(f"   {im:6} показов  поз. {pos:4.1f}  «{q}»  нет: {', '.join(missing(q, draft_stems))}")
    stems = draft_stems

print("\nВерхние запросы, слова которых не все в заголовках:")
shown = 0
for q, im, cl, pos in data:
    gone = missing(q, stems)
    if gone:
        print(f"   {im:6} показов  {cl:4} кликов  поз. {pos:4.1f}  «{q}»  нет: {', '.join(gone)}")
        shown += 1
        if shown >= args.top:
            break
if not shown:
    print("   все покрыты")
print(f"\nЗаголовков в текущей версии: {len(live_heads)}; title: {live_heads[0][:90] if live_heads else '-'}")
