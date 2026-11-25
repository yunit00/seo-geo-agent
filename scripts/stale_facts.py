#!/usr/bin/env python3
"""Устаревшие факты на сайте: старые цены, даты, условия, названия тарифов.

    stale_facts.py [--max-pages 300] [--today 2026-12-31]

Читает таблицу `facts.md` из рабочей папки сайта и ищет старые значения по всем
страницам: в папке исходников (`site_dir` в site.json) или на живом сайте по карте
сайта, плюс llms.txt. Поиск идет по HTML целиком, поэтому в него попадают title,
description и разметка schema.org. Перед поиском текст очищается от HTML-сущностей
и неразрывных пробелов: «4&nbsp;900» и «4 900» находятся одним выражением.

Отдельно печатаются факты с вышедшим сроком «Годен до» (акция, набор, сезонная
цена) и те, у которых срок выходит в ближайшую неделю.

Зачем. Факт меняется в одном месте, а повторяется на многих страницах: тексты
пишутся друг с друга, цена попадает в разметку и описание. Без поиска по всему
сайту старое значение живет месяцами.
"""
import argparse
import datetime as dt
import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _site  # noqa: E402
import _pages  # noqa: E402

NBSP = re.compile("[    ]")


def parse_table(text: str) -> list[dict]:
    """Строки таблицы фактов из 7 колонок. Шапка и блоки кода пропускаются."""
    facts, in_code = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        if len(cells) != 7 or cells[0] == "Факт":
            continue
        name, now, old, allow, until, source, checked = cells
        facts.append({
            "name": name, "now": now, "source": source, "checked": checked,
            "old": [re.compile(p, re.I) for p in re.findall(r"`([^`]+)`", old)],
            "allow": [re.compile(p, re.I) for p in re.findall(r"`([^`]+)`", allow)],
            "until": dt.date.fromisoformat(until) if until else None,
        })
    return facts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-pages", type=int, default=300, help="сколько страниц брать с живого сайта")
    ap.add_argument("--today", help="считать сроки на эту дату (ГГГГ-ММ-ДД)")
    args = ap.parse_args()
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()

    facts_file = _site.WORKDIR / "facts.md"
    if not facts_file.exists():
        print(f"!!! НЕ ИЗМЕРЕНО: нет файла фактов {facts_file}. Шаблон: templates/facts.md, "
              "заполняется вместе с владельцем.")
        return
    facts = parse_table(facts_file.read_text(encoding="utf-8"))
    if not facts:
        print(f"В {facts_file} нет ни одного факта. Заполнить с владельцем: цены, условия, сроки акций.")
        return

    hits = {f["name"]: [] for f in facts}
    pages = 0
    for label, text in _pages.iter_pages(args.max_pages):
        pages += 1
        for n, raw in enumerate(text.splitlines(), 1):
            line = NBSP.sub(" ", html.unescape(raw))
            for f in facts:
                if any(a.search(line) for a in f["allow"]):
                    continue
                for rx in f["old"]:
                    for m in rx.finditer(line):
                        s = max(0, m.start() - 50)
                        snippet = re.sub(r"\s+", " ", line[s:m.end() + 50]).strip()
                        hits[f["name"]].append((label, n, m.group(0), snippet))

    print(f"Факты: {facts_file}, источник: {_pages.describe()}, страниц {pages}, дата {today}\n")
    expired = [f for f in facts if f["until"] and f["until"] < today]
    soon = [f for f in facts if f["until"] and today <= f["until"] <= today + dt.timedelta(days=7)]
    if expired:
        print("СРОК ВЫШЕЛ, сверить с владельцем и обновить строку и страницы:")
        for f in expired:
            print(f"  - {f['name']}: «{f['now']}», годен до {f['until']} (где правда: {f['source']})")
        print()
    if soon:
        print("Срок выходит в ближайшие 7 дней:")
        for f in soon:
            print(f"  - {f['name']}: «{f['now']}», годен до {f['until']}")
        print()

    checked = [f for f in facts if f["old"]]
    total = sum(len(v) for v in hits.values())
    print(f"Старые значения на страницах: {total} (фактов с выражениями {len(checked)} из {len(facts)})")
    for f in checked:
        found = hits[f["name"]]
        print(f"\n{f['name']}: сейчас «{f['now']}», найдено {len(found)}")
        for label, n, val, snip in found[:15]:
            print(f"    {label}:{n}  [{val}]  …{snip}…")
        if len(found) > 15:
            print(f"    ... и еще {len(found) - 15} в {len({x[0] for x in found[15:]})} файлах")


if __name__ == "__main__":
    main()
