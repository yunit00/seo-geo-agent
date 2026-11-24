#!/usr/bin/env python3
"""Запросы «на расстоянии удара» в Google: нас уже показывают на позициях 8-20,
до первой страницы или до ее верха не хватает немного.

Это самый дешевый список тем для доработки: спрос подтвержден показами, страница
уже есть, конкуренты обходят ненамного. Для каждой страницы печатаются запросы,
по которым она стоит на 8-20 месте, с показами и кликами.

    gsc_reach.py [--days 90] [--min-impr 15] [--from 8] [--to 20]

Яндекс так смотреть тоже нужно, но через Вебмастер (`yandex.py queries`).
"""
import argparse
import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _site  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--days", type=int, default=90)
ap.add_argument("--min-impr", type=int, default=15)
ap.add_argument("--from", dest="lo", type=float, default=8)
ap.add_argument("--to", dest="hi", type=float, default=20)
args = ap.parse_args()

import gsc  # noqa: E402

api = gsc.service()
site = gsc.site_url()
end = dt.date.today() - dt.timedelta(days=3)          # данные Google отстают на три дня
start = end - dt.timedelta(days=args.days - 1)

body = {"startDate": start.isoformat(), "endDate": end.isoformat(),
        "dimensions": ["page", "query"], "rowLimit": 5000}
rows = api.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])

pages = defaultdict(list)
for r in rows:
    page, q = r["keys"]
    if args.lo <= r["position"] <= args.hi and r["impressions"] >= args.min_impr:
        pages[gsc.path_of(page)].append(
            (r["impressions"], r["clicks"], r["position"], q))

print(f"\nGoogle, {start} .. {end}: запросы на позициях {args.lo:g}-{args.hi:g} с показами от {args.min_impr}")
print(f"(всего строк страница+запрос в выгрузке: {len(rows)}; лимит 5000, длинный хвост может быть обрезан)\n")
if not pages:
    print("  таких запросов нет")
total = 0
for page, items in sorted(pages.items(), key=lambda kv: -sum(i[0] for i in kv[1])):
    items.sort(reverse=True)
    impr = sum(i[0] for i in items); total += impr
    print(f"{page}   показов {impr}, запросов {len(items)}")
    for im, cl, pos, q in items[:8]:
        print(f"    {im:5} показов  {cl:3} кликов  поз. {pos:4.1f}  {q[:70]}")
    if len(items) > 8:
        print(f"    ... и еще {len(items) - 8}")
print(f"\nвсего показов на этих позициях: {total}")
