#!/usr/bin/env python3
"""Сравнение периодов по логам сервера и Search Console: 7, 28 и 90 дней против
предыдущих таких же. Отвечает на три вопроса: сколько людей, что сдвинулось,
что делают нейросети с нашими страницами.

    periods.py [куда положить JSON] [--no-gsc]

Как считается.

Период заканчивается последним полным днем в архиве логов: сегодняшний день
неполный и в расчет не идет. Предыдущий период это такие же N дней прямо перед ним.

В архиве бывают дыры (хостинг хранит логи недолго, пропущенный забор это потеря),
поэтому у каждого периода печатается покрытие: сколько суток из N есть в архиве.
Посетители показаны средним в сутки по тем суткам, что есть. Редкие события
(переходы и чтения нейросетей, входы на страницу) показаны суммой, а прошлый период
приводится к числу суток текущего и помечается как оценка, если покрытие разное.
Покрытие прошлого периода ниже четверти: сравнения нет, так и пишется.

Чтение нейросетью это запрос страницы агентом ChatGPT-User, Claude-User или
Perplexity-User: нейросеть открыла страницу по ходу ответа человеку. Сюда попадает
и случай, когда человек сам вставил ссылку в чат, поэтому это верхняя оценка
цитирования. Обход это роботы индекса (GPTBot, OAI-SearchBot, ClaudeBot и другие).
Переход это живой человек, пришедший из интерфейса нейросети (реферер или utm_source).

Имя агента в логах можно подделать: сканеры уязвимостей ходят под видом ChatGPT-User
и просят /.env и подобное. Они получают 403, 404 или страницу ошибки, поэтому чтением
считается только ответ 200 на настоящую страницу, а страницы ошибок отбрасываются
(список в site.json, ключ not_pages).

Запросы-шум (site.json, ключ noise_queries: например, чужой продукт с похожим названием)
в списке выросших и упавших не показываются, их показы идут одной строкой.
"""
import datetime as dt
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _site  # noqa: E402
import logs  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]     # после импорта: _site уже убрал --site
NO_GSC = "--no-gsc" in sys.argv
OUT = args[0] if args else None


def engine(ref):
    m = re.match(r"https?://([^/]+)", ref or "")
    h = m.group(1).lower() if m else ""
    if "yandex" in h or "ya.ru" in h:
        return "Яндекс"
    if "google" in h:
        return "Google"
    return "др."


PERIODS = (7, 28, 90)
MIN_PREV_COVERAGE = 0.25      # ниже этого прошлый период не сравниваем

# Агент -> (нейросеть, вид запроса). Все, чего тут нет, идет в «прочие» обходом.
ASSISTANT = {
    "ChatGPT-User": ("ChatGPT", "reads"), "OAI-SearchBot": ("ChatGPT", "crawl"), "GPTBot": ("ChatGPT", "crawl"),
    "Claude-User": ("Claude", "reads"), "Claude-SearchBot": ("Claude", "crawl"), "ClaudeBot": ("Claude", "crawl"),
    "Perplexity-User": ("Perplexity", "reads"), "PerplexityBot": ("Perplexity", "crawl"),
}
CLICK_HOST = {"chatgpt.com": "ChatGPT", "chat.openai.com": "ChatGPT", "claude.ai": "Claude",
              "perplexity.ai": "Perplexity", "alice.yandex.ru": "Алиса", "ya.ru/alice": "Алиса",
              "search.yandex.ru/neuro": "Алиса", "gemini.google.com": "Gemini",
              "copilot.microsoft.com": "Copilot", "chat.deepseek.com": "DeepSeek", "giga.chat": "GigaChat"}
ORDER = ["ChatGPT", "Claude", "Perplexity", "Алиса"]
MEASURED = {who for who, _ in ASSISTANT.values()}

NOISE = [re.compile(x, re.I) for x in _site.SITE.get("noise_queries", [])]
NOT_PAGES = {"/404.html", "/404/", *_site.SITE.get("not_pages", [])}

TODAY = dt.date.today().isoformat()

# ---- один проход по архиву: посетители, чтения и обходы нейросетей
events, browser = [], logs.Browsers()
bot_hits = defaultdict(Counter)            # (нейросеть, вид) -> день -> запросов страниц
read_pages = defaultdict(Counter)          # день -> страница -> чтений
read_by = defaultdict(lambda: defaultdict(Counter))   # день -> страница -> нейросеть -> чтений
log_days = set()
for day, ip, url, code, ref, ua in logs.lines(None):
    if day >= TODAY:
        continue
    log_days.add(day)
    is_asset = bool(logs.ASSET.search(url))
    name = logs.crawler_name(ua)
    if name in ASSISTANT and not is_asset and code.startswith("2") and url.split("?")[0] not in NOT_PAGES:
        who, kind = ASSISTANT[name]
        bot_hits[(who, kind)][day] += 1
        if kind == "reads":
            page = url.split("?")[0]
            read_pages[day][page] += 1
            read_by[day][page][who] += 1
    if is_asset:
        if logs.BROWSER_ASSET.search(url):
            browser[day].add(ip)
        continue
    if logs.BOT.search(ua) or not code.startswith(("2", "3")):
        continue
    events.append((day, ip, url, ref))

if not log_days:
    sys.exit("В архиве нет ни одного полного дня логов")

# ---- первое касание за сутки
total = Counter()                           # день -> посетителей
by_src = defaultdict(Counter)               # день -> источник -> посетителей
by_eng = defaultdict(Counter)               # день -> поисковик -> посетителей
land = defaultdict(Counter)                 # день -> страница входа из поиска -> посетителей
ai_clicks = defaultdict(Counter)            # день -> нейросеть -> переходов
ai_click_pages = defaultdict(Counter)       # день -> страница -> переходов
seen = set()
for day, ip, url, ref in events:
    if ip not in browser[day] or (day, ip) in seen:
        continue
    seen.add((day, ip))
    host = logs.ai_origin(ref, url)
    src = logs.AI if host else (logs.source(ref) or logs.DIRECT)
    page = url.split("?")[0]
    total[day] += 1
    by_src[day][src] += 1
    if src == logs.SEARCH:
        by_eng[day][engine(ref)] += 1
        land[day][page] += 1

# Переход из нейросети считается отдельно от первого касания: человек мог утром зайти
# напрямую, а днем прийти по ссылке из ответа. Так же считает logs.py ai, цифры сходятся.
ai_seen = set()
for day, ip, url, ref in events:
    host = logs.ai_origin(ref, url)
    if not host or ip not in browser[day] or (day, ip) in ai_seen:
        continue
    ai_seen.add((day, ip))
    ai_clicks[day][CLICK_HOST.get(host, host)] += 1
    ai_click_pages[day][url.split("?")[0]] += 1

last = dt.date.fromisoformat(max(log_days))


def window(n, shift):
    """Сутки периода, которые есть в архиве, и границы периода."""
    end = last - dt.timedelta(days=shift)
    start = end - dt.timedelta(days=n - 1)
    want = [(start + dt.timedelta(days=i)).isoformat() for i in range(n)]
    return [d for d in want if d in log_days], start.isoformat(), end.isoformat()


def ssum(counter_by_day, days, key=None):
    if key is None:
        return sum(counter_by_day[d] if isinstance(counter_by_day[d], int) else sum(counter_by_day[d].values()) for d in days)
    return sum(counter_by_day[d][key] for d in days)


def merged(counter_by_day, days):
    out = Counter()
    for d in days:
        out.update(counter_by_day[d])
    return out


def per_day(value, days):
    return round(value / len(days), 1) if days else None


def movers(cur, prev, scale, k=5, floor=3):
    """Что выросло и что упало сильнее всего. prev приводится к числу суток текущего периода."""
    rows = []
    for key in set(cur) | set(prev):
        c, p = cur[key], round(prev[key] * scale)
        if abs(c - p) >= floor:
            rows.append({"key": key, "cur": c, "prev": p})
    up = sorted((r for r in rows if r["cur"] > r["prev"]), key=lambda r: r["prev"] - r["cur"])[:k]
    down = sorted((r for r in rows if r["cur"] < r["prev"]), key=lambda r: r["cur"] - r["prev"])[:k]
    return up, down


# ---- Search Console: запросы, выросшие и упавшие по показам
def gsc_movers():
    import gsc
    api = gsc.service()
    site = gsc.site_url()
    end = dt.date.today() - dt.timedelta(days=3)          # данные Google отстают на три дня
    out = {}
    for n in PERIODS:
        c1, c0 = end, end - dt.timedelta(days=n - 1)
        p1, p0 = c0 - dt.timedelta(days=1), c0 - dt.timedelta(days=n)

        def rows(a, b):
            body = {"startDate": a.isoformat(), "endDate": b.isoformat(), "dimensions": ["query"], "rowLimit": 1000}
            return {r["keys"][0]: r for r in api.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])}

        cur, prev = rows(c0, c1), rows(p0, p1)
        items, noise = [], [0, 0]
        for q in set(cur) | set(prev):
            c, p = cur.get(q), prev.get(q)
            ci, pi = (c["impressions"] if c else 0), (p["impressions"] if p else 0)
            if any(rx.search(q) for rx in NOISE):
                noise[0] += ci
                noise[1] += pi
                continue
            if abs(ci - pi) < max(10, n // 2):
                continue
            items.append({"key": q, "cur": ci, "prev": pi,
                          "clicks": c["clicks"] if c else 0, "clicksPrev": p["clicks"] if p else 0,
                          "pos": round(c["position"], 1) if c else None,
                          "posPrev": round(p["position"], 1) if p else None})
        up = sorted((r for r in items if r["cur"] > r["prev"]), key=lambda r: r["prev"] - r["cur"])[:5]
        down = sorted((r for r in items if r["cur"] < r["prev"]), key=lambda r: r["cur"] - r["prev"])[:5]
        out[n] = {"from": c0.isoformat(), "to": c1.isoformat(), "up": up, "down": down,
                  "queries": len(cur), "queriesPrev": len(prev), "noise": {"cur": noise[0], "prev": noise[1]}}
    return out


gsc_out, gsc_error = {}, None
if not _site.connected()["gsc"]:
    gsc_error = "Search Console не подключен"
elif not NO_GSC:
    try:
        gsc_out = gsc_movers()
    except (SystemExit, Exception) as e:  # noqa: BLE001
        gsc_error = f"{type(e).__name__}: {e}"[:200]

# ---- сборка периодов
result = {"lastDay": last.isoformat(), "firstDay": min(log_days), "logDays": sorted(log_days), "periods": {}}
for n in PERIODS:
    cd, c_from, c_to = window(n, 0)
    pd, p_from, p_to = window(n, n)
    has_prev = len(pd) >= n * MIN_PREV_COVERAGE
    scale = len(cd) / len(pd) if has_prev and pd else 0
    estimated = has_prev and len(pd) != len(cd)

    def pair_day(counter, key=None):
        return {"cur": per_day(ssum(counter, cd, key), cd), "prev": per_day(ssum(counter, pd, key), pd) if has_prev else None}

    def pair_sum(counter, key=None):
        return {"cur": ssum(counter, cd, key), "prev": round(ssum(counter, pd, key) * scale) if has_prev else None}

    t_cur, s_cur = ssum(total, cd), ssum(by_src, cd, logs.SEARCH)
    t_prev, s_prev = ssum(total, pd), ssum(by_src, pd, logs.SEARCH)
    traffic = {
        "total": pair_day(total), "search": pair_day(by_src, logs.SEARCH),
        "yandex": pair_day(by_eng, "Яндекс"), "google": pair_day(by_eng, "Google"),
        "direct": pair_day(by_src, logs.DIRECT), "social": pair_day(by_src, logs.SOCIAL),
        "groups": {g: pair_day(by_src, g) for g in logs.CUSTOM},
        "searchShare": {"cur": round(s_cur * 100 / t_cur) if t_cur else None,
                        "prev": round(s_prev * 100 / t_prev) if has_prev and t_prev else None},
        "searchSum": s_cur, "totalSum": t_cur,
    }
    land_cur, land_prev = merged(land, cd), merged(land, pd) if has_prev else Counter()
    top3 = sum(v for _, v in land_cur.most_common(3))
    traffic["top3Share"] = round(top3 * 100 / s_cur) if s_cur else None
    traffic["topPages"] = [{"key": u, "cur": v, "prev": round(land_prev[u] * scale) if has_prev else None,
                            "share": round(v * 100 / s_cur) if s_cur else 0} for u, v in land_cur.most_common(5)]
    up, down = movers(land_cur, land_prev, scale, floor=max(3, n // 7)) if has_prev else ([], [])

    names = ORDER + sorted({a for d in cd + pd for a in ai_clicks[d]} - set(ORDER))
    assistants = []
    for who in names:
        # У Алисы и прочих нет своего агента в логах: чтения и обходы для них не измеряются, это не ноль.
        seen_bot = who in MEASURED
        nothing = {"cur": None, "prev": None}
        row = {"name": who, "reads": pair_sum(bot_hits[(who, "reads")]) if seen_bot else nothing,
               "crawl": pair_sum(bot_hits[(who, "crawl")]) if seen_bot else nothing,
               "clicks": pair_sum(ai_clicks, who)}
        if seen_bot or row["clicks"]["cur"] or row["clicks"]["prev"]:
            assistants.append(row)
    reads_cur = merged(read_pages, cd)
    clicks_cur = merged(ai_click_pages, cd)
    who_cur = defaultdict(Counter)
    for d in cd:
        for page, c in read_by[d].items():
            who_cur[page].update(c)
    reads_prev = merged(read_pages, pd) if has_prev else Counter()
    ai = {
        "reads": {"cur": sum(reads_cur.values()), "prev": round(sum(reads_prev.values()) * scale) if has_prev else None},
        "clicks": pair_sum(ai_clicks),
        "assistants": assistants,
        "readPages": [{"key": u, "reads": v, "prev": round(reads_prev[u] * scale) if has_prev else None,
                       "clicks": clicks_cur[u], "by": dict(who_cur[u])} for u, v in reads_cur.most_common(6)],
        "clickPages": [{"key": u, "clicks": v} for u, v in clicks_cur.most_common(5)],
    }

    g = gsc_out.get(n)
    result["periods"][str(n)] = {
        "days": n, "from": c_from, "to": c_to, "prevFrom": p_from, "prevTo": p_to,
        "daysHave": len(cd), "prevDaysHave": len(pd), "hasPrev": has_prev, "prevEstimated": estimated,
        "traffic": traffic, "ai": ai,
        "movers": {"pages": {"up": up, "down": down},
                   "queries": ({"ok": True, **g} if g else {"ok": False, "error": gsc_error or "Search Console не опрашивался"})},
    }

# ---- человеку
def fmt(pair, unit=""):
    c, p = pair["cur"], pair["prev"]
    if c is None:
        return "не измеряется"
    if p is None:
        return f"{c}{unit} (сравнить не с чем)"
    d = f"{(c - p) * 100 / p:+.0f}%" if p and max(c, p) >= 30 else f"было {p}"
    return f"{c}{unit} ({d})"


print(f"Сравнение периодов по логам. Последний полный день: {last}. В архиве {len(log_days)} суток с {min(log_days)}.\n")
for n in PERIODS:
    p = result["periods"][str(n)]
    t, a = p["traffic"], p["ai"]
    cover = f"суток в архиве {p['daysHave']} из {n}, в прошлом периоде {p['prevDaysHave']} из {n}"
    note = "" if p["hasPrev"] else "  СРАВНЕНИЯ НЕТ: прошлый период в архиве почти пуст."
    if p["prevEstimated"]:
        note = "  Покрытие разное: суммы прошлого периода приведены к числу суток текущего (оценка)."
    print(f"== {n} дней: {p['from']} .. {p['to']} против {p['prevFrom']} .. {p['prevTo']}  ({cover}){note}")
    print(f"  в сутки: всего {fmt(t['total'])}, из поиска {fmt(t['search'])}, Яндекс {fmt(t['yandex'])}, Google {fmt(t['google'])}, "
          f"прямые и мессенджеры {fmt(t['direct'])}, соцсети {fmt(t['social'])}"
          + "".join(f", {g} {fmt(v)}" for g, v in t["groups"].items()))
    print(f"  доля поиска {t['searchShare']['cur']}% (было {t['searchShare']['prev']}%), три верхние страницы дают {t['top3Share']}% входов из поиска")
    for label, rows in (("выросли", p["movers"]["pages"]["up"]), ("упали", p["movers"]["pages"]["down"])):
        if rows:
            print(f"  страницы входа из поиска, {label}: " + "; ".join(f"{r['key']} {r['prev']} -> {r['cur']}" for r in rows))
    q = p["movers"]["queries"]
    if q["ok"]:
        print(f"  Google, запросы по показам ({q['from']} .. {q['to']}):")
        for label, rows in (("выросли", q["up"]), ("упали", q["down"])):
            for r in rows:
                print(f"    {label:7} {r['prev']:5} -> {r['cur']:5} показов, поз. {r['posPrev']} -> {r['pos']}, кликов {r['clicks']}  {r['key'][:60]}")
        if q["noise"]["prev"] or q["noise"]["cur"]:
            print(f"    запросы-шум: {q['noise']['prev']} -> {q['noise']['cur']} показов, в список не входят")
    else:
        print(f"  Google, запросы: не измерено ({q['error']})")
    print(f"  нейросети: чтений {fmt(a['reads'])}, переходов {fmt(a['clicks'])}")
    for r in a["assistants"]:
        print(f"    {r['name']:11} чтений {fmt(r['reads'])}, переходов {fmt(r['clicks'])}, обходов {fmt(r['crawl'])}")
    if a["readPages"]:
        print("    чаще всего читали: " + "; ".join(f"{r['key']} {r['reads']} (переходов {r['clicks']})" for r in a["readPages"]))
    print()
print("Оговорка: чтение считается и тогда, когда человек сам вставил ссылку в чат. Это верхняя оценка цитирования.")

if OUT:
    Path(OUT).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
