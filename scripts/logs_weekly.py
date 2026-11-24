#!/usr/bin/env python3
"""Логи сервера по неделям: источники, поисковики, страницы входа из поиска.

Дополняет logs.py (тот дает сводку за период, этот показывает динамику). Считает
живых посетителей той же методикой: первое касание за сутки.

    logs_weekly.py [куда положить JSON]

Среднее в сутки, а не сумма за неделю: в архиве бывают дыры, и суммы неполных
недель вводят в заблуждение.

Сегодняшний день в расчет не берется: он неполный. Неполный день, посчитанный
как полный, занижает текущую неделю и дает ложный вывод о спаде.

Неполную текущую неделю нельзя сравнивать с полными напрямую: в ней нет выходных.
Поэтому отдельной строкой печатается сравнение тех же дней недели с прошлой неделей.
"""
import datetime as dt
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import logs  # noqa: E402

OUT = next((a for a in sys.argv[1:] if not a.startswith("--")), None)


def engine(ref):
    m = re.match(r"https?://([^/]+)", ref or "")
    h = m.group(1).lower() if m else ""
    if "yandex" in h or "ya.ru" in h:
        return "Яндекс"
    if "google" in h:
        return "Google"
    if "bing" in h:
        return "Bing"
    return "др."


def week(day):
    d = dt.date.fromisoformat(day)
    return (d - dt.timedelta(days=d.weekday())).isoformat()


TODAY = dt.date.today().isoformat()
events, browser = logs.load(None)
first, landing, refs = {}, {}, {}
for day, ip, url, ref in events:
    if day >= TODAY or ip not in browser[day]:
        continue
    k = (day, ip)
    if k in first:
        continue
    first[k] = logs.AI if logs.ai_origin(ref, url) else (logs.source(ref) or logs.DIRECT)
    landing[k] = url.split("?")[0]
    refs[k] = ref

if not first:
    logs.hint_no_assets(events, first)
    sys.exit("В архиве нет ни одного полного дня с посетителями.")

days = defaultdict(set)
src, eng, land = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
for k, s in first.items():
    w = week(k[0])
    days[w].add(k[0])
    src[w][s] += 1
    if s == logs.SEARCH:
        eng[w][engine(refs[k])] += 1
        land[w][landing[k]] += 1

# Строка для сторожа: последние 7 полных суток против предыдущих 7 (дни недели совпадают сами).
all_days = sorted({k[0] for k in first})
last_day = dt.date.fromisoformat(all_days[-1])


def span(shift):
    want = {(last_day - dt.timedelta(days=i + shift)).isoformat() for i in range(7)}
    have = want & set(all_days)
    return (sum(1 for k, s in first.items() if k[0] in have and s == logs.SEARCH),
            sum(1 for k in first if k[0] in have), len(have))


s1, t1, n1 = span(0)
s0, t0, n0 = span(7)
print(f"WATCH logs_last_day={all_days[-1]} search_7d={s1} search_prev7d={s0} total_7d={t1} total_prev7d={t0} days_7d={n1} days_prev7d={n0}\n")

weeks = sorted(src)
rest = [g for g in logs.groups() if g not in (logs.DIRECT, logs.SEARCH, logs.AI)]
print("Живые посетители, среднее в сутки по неделям (неделя с понедельника)\n")
print("неделя      дн  всего  прямые  поиск  остальные  AI (сумма)  доля поиска | Яндекс  Google  Bing")
for w in weeks:
    n = len(days[w]); c = src[w]; t = sum(c.values()); e = eng[w]
    other = sum(c[g] for g in rest)
    print(f"{w}  {n}  {t/n:5.0f}  {c[logs.DIRECT]/n:6.0f}  {c[logs.SEARCH]/n:5.0f}  {other/n:9.0f}"
          f"  {c[logs.AI]:10}  {c[logs.SEARCH]*100/t:10.0f}% | {e['Яндекс']/n:6.1f}  {e['Google']/n:6.1f}  {e['Bing']/n:4.1f}")
print(f"\nпрямые это «{logs.DIRECT}» (заходы без реферера); остальные: " + ", ".join(rest))

# Текущая неделя против тех же дней прошлой: честное сравнение для неполной недели.
last = weeks[-1]
if len(days[last]) < 7 and len(weeks) > 1:
    prev = (dt.date.fromisoformat(last) - dt.timedelta(days=7)).isoformat()
    same = {(dt.date.fromisoformat(d) - dt.timedelta(days=7)).isoformat() for d in days[last]}
    if prev in src and same <= days[prev]:
        def count(day_set, what):
            return sum(1 for k, s in first.items() if k[0] in day_set and (what is None or s == what))
        n = len(days[last])
        print(f"\nТекущая неделя неполная ({n} дн.). Те же дни недели, эта неделя против прошлой, в сутки:")
        for label, what in (("всего", None), ("поиск", logs.SEARCH), ("прямые", logs.DIRECT)):
            a, b = count(days[last], what) / n, count(same, what) / n
            print(f"  {label:10} {a:6.0f} против {b:6.0f}  ({(a - b) * 100 / b if b else 0:+.0f}%)")
        la, lb = Counter(), Counter()
        for k, s in first.items():
            if s == logs.SEARCH:
                if k[0] in days[last]:
                    la[landing[k]] += 1
                elif k[0] in same:
                    lb[landing[k]] += 1
        moved = sorted(set(la) | set(lb), key=lambda u: -abs(la[u] - lb[u]))[:5]
        print("  что изменилось во входах из поиска: " + "; ".join(f"{u} {lb[u]} -> {la[u]}" for u in moved))
    else:
        print(f"\nТекущая неделя неполная ({len(days[last])} дн.), а за те же дни прошлой недели логов нет: сравнивать не с чем.")

recent = [w for w in weeks if len(days[w]) >= 4][-5:] or weeks[-1:]
tot = Counter()
for w in recent:
    tot.update(land[w])
total = sum(tot.values()) or 1
print(f"\nСтраницы входа из поиска, посетителей за неделю (недели с {recent[0]}; у крайней может быть меньше 7 дней)\n")
print("страница".ljust(52) + "".join(w[5:].rjust(7) for w in recent) + "  итого  доля")
for url, n in tot.most_common(30):
    print(url[:50].ljust(52) + "".join(str(land[w][url]).rjust(7) for w in recent) + f"  {n:5}  {n*100/total:3.0f}%")
top3 = sum(n for _, n in tot.most_common(3))
print(f"\nКонцентрация за показанные недели (с {recent[0]}): три верхние страницы дают {top3*100/total:.0f}% поисковых входов.")

if OUT:
    out = {"lastDay": all_days[-1],
           "watch": {"search7d": s1, "searchPrev7d": s0, "total7d": t1, "totalPrev7d": t0, "days7d": n1, "daysPrev7d": n0},
           "weeks": [{"week": w, "days": len(days[w]),
                      "totalPerDay": round(sum(src[w].values()) / len(days[w]), 1),
                      "searchPerDay": round(src[w][logs.SEARCH] / len(days[w]), 1),
                      "yandexPerDay": round(eng[w]["Яндекс"] / len(days[w]), 1),
                      "googlePerDay": round(eng[w]["Google"] / len(days[w]), 1),
                      "directPerDay": round(src[w][logs.DIRECT] / len(days[w]), 1),
                      "aiClicks": src[w][logs.AI]} for w in weeks],
           "topSearchPages": [{"url": u, "visitors": n, "share": round(n * 100 / total)} for u, n in tot.most_common(8)],
           "topSearchPagesSince": recent[0],
           "top3Share": round(top3 * 100 / total)}
    Path(OUT).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
