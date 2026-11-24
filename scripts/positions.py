#!/usr/bin/env python3
"""Трекер позиций в Яндексе: место сайта по списку запросов, неделя к неделе.

Панель Яндекса показывает позиции с задержкой и может неделями не обновляться,
поэтому место в выдаче снимаем сами через Yandex Search API (тот же ключ, что у
Wordstat). Список запросов: queries.txt в рабочей папке сайта.

    positions.py            снять и сравнить с прошлым снимком
    positions.py --show     показать последний снимок, ничего не снимая
    positions.py --depth 50 глубина выдачи (по умолчанию 50)

Выдача Яндекса по части запросов скачет от запроса к запросу: поисковик пробует
страницу на высоком месте и убирает (один и тот же запрос может дать 8-е место,
а два следующих раза сайта в первых 50 не будет). Поэтому новое или резко
изменившееся место перепроверяется еще двумя запросами, в снимок идет медиана, а
запрос помечается как неустойчивый. Устойчивые места снимаются одним запросом.

Запросы к API платные: 35 фраз это 35-80 запросов за запуск. Снимки лежат в
<настройки сайта>/positions/<дата>.json. Если сайт стоит не той страницей, что указана
в списке, это печатается: значит, поисковик считает релевантной другую.
"""
import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _site  # noqa: E402
import wordstat  # noqa: E402

LIST = _site.WORKDIR / "queries.txt"
STORE = _site.CONFIG_DIR / "positions"
DOMAIN = _site.DOMAIN


def queries():
    out = []
    if not LIST.exists():
        sys.exit(f"Нет списка запросов: {LIST}. Составь его в онбординге (формат: запрос | /страница/).")
    for line in LIST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        q, _, page = (p.strip() for p in line.partition("|"))
        out.append((q, None if page in ("", "-") else page))
    return out


def serp(q: str, depth: int):
    j = wordstat.post("/v2/web/search", {
        "query": {"searchType": "SEARCH_TYPE_RU", "queryText": q},
        "groupSpec": {"groupsOnPage": depth}, "region": wordstat.REGION, "responseFormat": "FORMAT_XML"})
    import base64
    return wordstat.parse_serp(base64.b64decode(j["rawData"]).decode("utf-8", "replace"))


def show(cur, prev):
    was = {r["query"]: r for r in prev.get("rows", [])}
    print(f"\nПозиции в Яндексе, {cur['date']}" + (f" (сравнение с {prev['date']})" if prev.get("date") else " (первый снимок)"))
    print(f"глубина выдачи {cur['depth']}; «—» значит, что в первых {cur['depth']} сайта нет\n")
    for r in sorted(cur["rows"], key=lambda r: (r["position"] is None, r["position"] or 999)):
        p = r["position"]
        old = was.get(r["query"], {}).get("position")
        if r["query"] not in was:
            d = ""
        elif p == old:
            d = "  ="
        elif p is None:
            d = f"  выпал (был {old})"
        elif old is None:
            d = "  вошел"
        else:
            d = f"  {'+' if old > p else '-'}{abs(old - p)}"
        note = ""
        if p and r.get("page") and r.get("found") and r["page"] not in r["found"]:
            note = f"   стоит другой страницей: {r['found'].split(DOMAIN, 1)[-1] or '/'}"
        if r.get("unstable"):
            note += "   неустойчиво: " + ", ".join(str(s) if s else "—" for s in r["samples"])
        print(f"  {str(p) if p else '—':>3}{d:16}  {r['query'][:46]:46}{note}")
    rows = cur["rows"]
    k = lambda n: sum(1 for r in rows if r["position"] and r["position"] <= n)  # noqa: E731
    print(f"\nвсего запросов {len(rows)}: в топ-3 {k(3)}, в топ-10 {k(10)}, в топ-30 {k(30)}, вне первых {cur['depth']}: {sum(1 for r in rows if not r['position'])}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--depth", type=int, default=50)
    args = ap.parse_args()
    STORE.mkdir(parents=True, exist_ok=True)
    files = sorted(STORE.glob("*.json"))
    today = dt.date.today().isoformat()

    if args.show:
        if not files:
            sys.exit("Снимков еще нет.")
        cur = json.loads(files[-1].read_text())
        prev = json.loads(files[-2].read_text()) if len(files) > 1 else {}
        return show(cur, prev)

    prev_files = [f for f in files if f.stem != today]
    prev = json.loads(prev_files[-1].read_text()) if prev_files else {}
    was = {r["query"]: r.get("position") for r in prev.get("rows", [])}

    def once(q):
        docs = serp(q, args.depth)
        time.sleep(1.1)   # лимит API: один синхронный запрос в секунду
        for i, d in enumerate(docs, 1):
            if DOMAIN in d["domain"]:
                return i, d["url"], docs
        return None, None, docs

    todo = queries()
    if not todo:
        sys.exit(f"Список запросов пуст: {LIST}")
    rows = []
    for q, page in todo:
        pos, found, docs = once(q)
        samples = [pos]
        old = was.get(q, "new")
        settled = old != "new" and ((pos is None and old is None) or (pos and old and abs(pos - old) <= 3))
        if not settled and not (old == "new" and pos is None):
            # Новое или сдвинувшееся место: два контрольных запроса, в снимок идет медиана.
            for _ in range(2):
                p2, f2, _d = once(q)
                samples.append(p2)
                found = found or f2
            ordered = sorted(samples, key=lambda x: x if x else 10 ** 6)
            pos = ordered[1]
        rows.append({"query": q, "page": page, "position": pos, "found": found if pos else None,
                     "samples": samples, "unstable": len({s if s else 0 for s in samples}) > 1 and
                     (max(s or 999 for s in samples) - min(s or 999 for s in samples) > 5),
                     "top3": [d["domain"] for d in docs[:3]]})
    cur = {"date": today, "depth": args.depth, "rows": rows}
    (STORE / f"{today}.json").write_text(json.dumps(cur, ensure_ascii=False, indent=1))
    show(cur, prev)


if __name__ == "__main__":
    main()
