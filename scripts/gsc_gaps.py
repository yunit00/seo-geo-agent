#!/usr/bin/env python3
"""Две дыры в Google, которые не видны по одной странице: двойники и слабый CTR.

    gsc_gaps.py [--days 90] [--min-impr 30] [--share 0.2] [--with-hubs]

1. Двойники. Запросы, по которым Google делит показы между двумя и более
   страницами сайта (у каждой не меньше --share показов запроса). Обычно ни одна
   из них не стоит высоко: поисковик не может выбрать. Лечится разведением
   (у каждой свой title и свое обещание, ссылка с одной на другую) или склейкой
   через 301. Перед склейкой сравнить места по общим запросам: страницу, которая
   стоит выше, в другую не вливать. Хабы из site.json (`hub_pages`, обычно главная
   и каталог статей) по умолчанию не считаются двойниками, --with-hubs их покажет.

2. Слабый CTR. Страницы на первой странице выдачи (позиция до 8), у которых
   кликов меньше половины от того, что сам сайт получает на тех же позициях.
   Сравнение с собственной кривой сайта, отраслевые таблицы CTR не используются.
   «Недобор» это ожидаемые клики минус фактические, порядок величины, не прогноз.
   Таким страницам нужен новый title и description; текст в том же заходе не
   трогать, иначе замер не скажет, что сработало.

Брендовые и шумовые запросы (`brand_queries`, `noise_queries` в site.json)
отсеиваются: кто ищет по имени бренда, придет и так, а кривую CTR такие
запросы задирают.
"""
import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _site  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--days", type=int, default=90)
ap.add_argument("--min-impr", type=int, default=30)
ap.add_argument("--share", type=float, default=0.2)
ap.add_argument("--with-hubs", action="store_true")
args = ap.parse_args()

import gsc  # noqa: E402

brand = [re.compile(p, re.I) for p in _site.SITE.get("brand_queries", [])]
noise = [re.compile(p, re.I) for p in _site.SITE.get("noise_queries", [])]
hubs = set(_site.SITE.get("hub_pages", ["/"]))

rows, start, end = gsc.all_rows(args.days, ["query", "page"])
data, dropped = [], 0
for r in rows:
    q, url = r["keys"]
    if any(x.search(q) for x in brand + noise):
        dropped += 1
        continue
    data.append((q, gsc.path_of(url), r["impressions"], r["clicks"], r["position"]))

print(f"Google, {start} .. {end}: строк запрос+страница {len(rows)}, без бренда и шума {len(data)} (отсеяно {dropped})")
print("Редкие запросы Google в разбивку не отдает, часть показов в этих таблицах не видна.\n")

# 1. Двойники
by_query = defaultdict(list)
for q, p, im, cl, pos in data:
    by_query[q].append((im, cl, pos, p))
doubles = []
for q, items in by_query.items():
    total = sum(i[0] for i in items)
    if total < args.min_impr:
        continue
    strong = [i for i in items if i[0] / total >= args.share and (args.with_hubs or i[3] not in hubs)]
    if len(strong) >= 2:
        doubles.append((total, q, sorted(strong, reverse=True)))
doubles.sort(reverse=True)

print(f"1. ДВОЙНИКИ: запросы от {args.min_impr} показов, где у двух и более страниц не меньше {args.share:.0%} показов")
if not doubles:
    print("   не найдено")
pairs = defaultdict(lambda: [0, 0])
for total, q, items in doubles[:30]:
    print(f"\n   «{q}», показов {total}")
    for im, cl, pos, p in items:
        print(f"      {p:55} {im:5} показов ({im / total:4.0%})  {cl:3} кликов  поз. {pos:4.1f}")
    key = tuple(sorted(i[3] for i in items))
    pairs[key][0] += total
    pairs[key][1] += 1
if pairs:
    print("\n   Пары страниц, сумма показов спорных запросов:")
    for key, (impr, n) in sorted(pairs.items(), key=lambda kv: -kv[1][0])[:10]:
        print(f"      {impr:6}  {' + '.join(key)}  ({n} запр.)")
print()

# 2. Слабый CTR против кривой самого сайта
def bucket(pos: float) -> int:
    return min(int(pos + 0.5), 10)


curve = defaultdict(lambda: [0, 0])
for q, p, im, cl, pos in data:
    curve[bucket(pos)][0] += im
    curve[bucket(pos)][1] += cl
ctr = {b: (c / i if i else 0) for b, (i, c) in curve.items()}
print("2. СЛАБЫЙ CTR. Кривая сайта по позициям (показы, клики, CTR):")
for b in range(1, 11):
    i, c = curve.get(b, [0, 0])
    if i:
        print(f"      {b:>2}{'+' if b == 10 else ' '}  {i:7} показов {c:5} кликов  {ctr[b]:5.1%}")

pages = defaultdict(lambda: [0, 0, 0.0, 0.0])
for q, p, im, cl, pos in data:
    if pos > 8:
        continue
    acc = pages[p]
    acc[0] += im
    acc[1] += cl
    acc[2] += im * ctr[bucket(pos)]
    acc[3] += im * pos
weak = sorted(((exp - cl, p, im, cl, exp, wpos / im) for p, (im, cl, exp, wpos) in pages.items()
               if im >= args.min_impr * 4 and exp > 0 and cl < exp / 2), reverse=True)
print(f"\n   Страницы с показами на позициях до 8 от {args.min_impr * 4} и кликами меньше половины ожидаемых:")
if not weak:
    print("   не найдено")
for miss, p, im, cl, exp, pos in weak[:15]:
    print(f"      {p:55} {im:6} показов  {cl:4} кликов  ожидалось {exp:6.1f}  недобор {miss:6.1f}  ср. поз. {pos:4.1f}")
print("\nНа десятках кликов разница легко случайна: это кандидаты, а не вывод.")
