#!/usr/bin/env python3
"""Сторож: ежедневная проверка без ИИ. Молчит, пока все в порядке.

Снимает свежие данные (тот же снимок, что у пульса), проверяет грубые тревоги и
сообщает о каждой новой. Одна и та же тревога повторно не сообщается, пока не
прошла неделя; исчезнувшая снимается из состояния.

    watch.py          проверить и сообщить о новых тревогах
    watch.py --dry    только показать, о чем сообщил бы

Что проверяет:
  - источник данных не ответил (мертвый токен, сбой API);
  - логи сервера перестали приходить;
  - поиск за 7 суток упал больше чем на 30% к предыдущим 7 (по логам);
  - лиды за 7 суток упали вдвое к среднему (если настроен leads_command);
  - ключевые страницы сайта не отдают 200 (watch_urls в site.json);
  - известная поломка починилась (хорошая новость, но о ней тоже надо узнать).

Источник данных считается упавшим, только если не ответил два дня подряд: у
сервисов бывают технические работы на несколько часов, и разовый отказ тревогой
не является. Недоступная страница сайта и падение трафика сообщаются сразу.

Куда сообщает. Всегда дописывает строку в <настройки сайта>/watch/alerts.log:
его агент читает в начале пульса. Если в site.json задан alert_command, еще и
выполняет эту команду; текст тревоги приходит в переменной окружения
SEO_ALERT_TEXT и подставляется вместо {text}. Так тревогу можно отправить в
мессенджер, в планировщик задач или на почту.

Пороги нарочно грубые: сторож ловит поломки, а не колебания. Известные поломки,
о которых владелец уже знает, перечисляются в site.json (known_issues, имена
источников из манифеста снимка) и тревог не создают.

Раз в неделю заодно снимает места в Яндексе (positions.py), если подключен ключ
Yandex Search API и в site.json стоит "watch_positions": true. Запросы платные,
поэтому по умолчанию выключено.
"""
import argparse
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _site  # noqa: E402

import requests  # noqa: E402

WATCH = _site.CONFIG_DIR / "watch"
STATE = WATCH / "state.json"
ALERTS = WATCH / "alerts.log"
PULSE = _site.CONFIG_DIR / "pulse"
RENOTIFY_DAYS = 7


def watch_line(path: Path) -> dict:
    """Строка вида `WATCH ключ=значение ...` из вывода источника."""
    if not path.exists():
        return {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("WATCH "):
            return dict(kv.split("=", 1) for kv in line[6:].split() if "=" in kv)
    return {}


def num(d, k):
    try:
        return float(d[k])
    except (KeyError, ValueError):
        return None


def collect(today: str):
    """Возвращает (тревоги, хорошие новости): списки (ключ, текст)."""
    alarms, good = [], []
    known = set(_site.SITE.get("known_issues", []))
    site = ["--site", _site.DOMAIN]

    if (_site.SITE.get("logs") or {}).get("source", "none") != "none":
        r = subprocess.run([sys.executable, str(HERE / "logs.py"), "sync", *site],
                           capture_output=True, text=True, timeout=1200)
        if r.returncode != 0:
            alarms.append(("logs_sync", "логи не забрались: " + (r.stderr or r.stdout).strip()[-150:]))

    if _site.SITE.get("watch_positions") and _site.connected()["wordstat"]:
        pos_dir = _site.CONFIG_DIR / "positions"
        last_pos = max((f.stem for f in pos_dir.glob("*.json")), default="2000-01-01")
        if (dt.date.fromisoformat(today) - dt.date.fromisoformat(last_pos)).days >= 7:
            r = subprocess.run([sys.executable, str(HERE / "positions.py"), *site],
                               capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                alarms.append(("positions", "трекер позиций не отработал: " + (r.stderr or r.stdout).strip()[-150:]))

    p = subprocess.run([sys.executable, str(HERE / "snapshot.py"), "--pulse", *site],
                       capture_output=True, text=True, timeout=2400)
    folder = PULSE / today
    mf = folder / "manifest.json"
    if not mf.exists():
        return [("snapshot", "снимок не снялся: " + (p.stderr or p.stdout).strip()[-200:])], good
    manifest = json.loads(mf.read_text(encoding="utf-8"))

    failed = {s["name"]: s for s in manifest["sources"] if not s["ok"]}
    for name, s in failed.items():
        if name in known:
            continue
        alarms.append((f"source:{name}", f"источник {name} ({s['group']}) не ответил: {s['reason'][:120]}"))
    for name in known:
        if name not in failed and any(s["name"] == name for s in manifest["sources"]):
            good.append((f"fixed:{name}", f"{name} снова отдает данные, пора дозамерить то, что ждало"))

    lg = watch_line(folder / "logs_weekly.txt")
    if lg:
        last = lg.get("logs_last_day", "")
        if last and (dt.date.fromisoformat(today) - dt.date.fromisoformat(last)).days > 2:
            alarms.append(("logs_stale", f"логи сервера не приходят: последний день в архиве {last}"))
        s1, s0, n1, n0 = num(lg, "search_7d"), num(lg, "search_prev7d"), num(lg, "days_7d"), num(lg, "days_prev7d")
        if s1 is not None and s0 and n1 == 7 and n0 == 7 and s0 >= 100 and s1 < 0.7 * s0:
            alarms.append(("search_drop", f"поиск упал: {s1:.0f} посетителей за 7 суток против {s0:.0f} "
                                          f"за предыдущие ({(s1 / s0 - 1) * 100:+.0f}%)"))

    ld = watch_line(folder / "leads.txt")
    if ld:
        cur, avg = num(ld, "leads_7d"), num(ld, "leads_prev_week_avg")
        if cur is not None and avg and avg >= 4 and cur < 0.5 * avg:
            alarms.append(("leads_drop", f"лиды упали: {cur:.0f} за 7 суток при среднем {avg:.1f} в неделю"))

    for path in _site.SITE.get("watch_urls") or ["/"]:
        url = path if path.startswith("http") else _site.site_url() + path
        try:
            code = requests.get(url, timeout=20, headers={"User-Agent": "seo-geo-agent-watch"}).status_code
        except Exception as e:  # noqa: BLE001
            code = type(e).__name__
        if code != 200:
            alarms.append((f"url:{path}", f"страница {path} отвечает {code}"))
    return alarms, good


def notify(text: str, today: str, dry: bool) -> bool:
    line = f"SEO-сторож, {_site.DOMAIN}: {text}"
    if dry:
        print("  [вхолостую]", line)
        return True
    WATCH.mkdir(parents=True, exist_ok=True)
    with ALERTS.open("a", encoding="utf-8") as f:
        f.write(f"{today}\t{text}\n")
    print("  тревога:", line)
    command = _site.SITE.get("alert_command")
    if command:
        r = subprocess.run(command.replace("{text}", shlex.quote(line)), shell=True, capture_output=True,
                           text=True, timeout=120, env={**os.environ, "SEO_ALERT_TEXT": line})
        if r.returncode != 0:
            print("  команда оповещения не сработала:", (r.stderr or r.stdout).strip()[-160:])
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    today = dt.date.today().isoformat()

    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    alarms, good = collect(today)
    print(f"Сторож {today}: тревог {len(alarms)}, хороших новостей {len(good)}")

    live = set()
    for key, text in alarms + good:
        live.add(key)
        seen = state.get(key)
        if key.startswith("source:") and (not seen or (seen.get("pending") and seen["first"] == today)):
            # Первый день отказа источника: запомнить и подождать до завтра.
            print(f"  первый отказ, жду второго дня: {text}")
            if not args.dry:
                state[key] = {"first": today, "pending": True, "text": text}
            continue
        if seen and seen.get("notified") and \
                (dt.date.fromisoformat(today) - dt.date.fromisoformat(seen["notified"])).days < RENOTIFY_DAYS:
            print(f"  уже сообщал {seen['notified']}: {text}")
            continue
        if notify(text, today, args.dry) and not args.dry:
            state[key] = {"first": (seen or {}).get("first", today), "notified": today, "text": text}
    for key in [k for k in state if k not in live]:
        print(f"  снято: {state[key]['text']}")
        if not args.dry:
            del state[key]
    if not args.dry:
        WATCH.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    if not alarms and not good:
        print("  все в порядке")


if __name__ == "__main__":
    main()
