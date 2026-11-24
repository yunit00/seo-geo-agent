#!/usr/bin/env python3
"""Снимок всех подключенных источников данных одной командой.

Запускает скрипты источников, складывает их вывод в папку снимка и пишет
manifest.json: какой источник отработал, какой нет и почему. Смысл манифеста:
упавший источник должен быть виден в отчете как упавший, а не молча выпасть
или быть заполненным по памяти. Неподключенные источники перечисляются отдельно:
это не сбой, но и не ноль.

    snapshot.py                 снимок для замера: <рабочая папка>/<дата>/raw/
    snapshot.py --full          плюс сверка индексации по карте сайта (долго)
    snapshot.py --pulse         снимок для пульса: <настройки сайта>/pulse/<дата>/,
                                плюс окна за 7 дней и кандидаты для рекомендаций
    snapshot.py --date 2026-01-15
    snapshot.py --force         снять заново, даже если свежий снимок уже есть

Снимок моложе трех часов не переснимается: скрипт печатает его состояние и выходит.
Повторный запуск в ту же папку не теряет то, что снято раньше: манифест дополняется.

Ничего не меняет ни на сайте, ни в сервисах: только читает.
"""
import argparse
import datetime as dt
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _site  # noqa: E402

PULSE_DIR = _site.CONFIG_DIR / "pulse"
REUSE_HOURS = 3
NOT_MEASURED = "!!! НЕ ИЗМЕРЕНО"

# Признаки того, что команда формально завершилась, но данных не дала.
FAIL_MARKS = ("429:", "503:", "Quota exceeded", "Service unavailable", "Invalid oauth_token",
              "токен недействителен", "нет нужного доступа", "недоступно:",
              "Traceback (most recent call last)", "timed out", "Read timed out", "ConnectionError")


def jobs(full: bool, pulse: bool, out: Path):
    py = sys.executable
    site = ["--site", _site.DOMAIN]
    linked = _site.connected()
    j, missing = [], []

    def add(name, group, script, *args, timeout=150):
        j.append({"name": name, "group": group, "timeout": timeout,
                  "cmd": [py, str(HERE / script), *[str(a) for a in args], *site]})

    if linked["metrika"]:
        for c in ("summary", "sources", "pages", "search", "conversions"):
            add(f"metrika_{c}", "Метрика", "yandex.py", c, "--days", 30)
        if pulse:
            for c in ("summary", "sources", "conversions"):
                add(f"metrika_{c}_7", "Метрика", "yandex.py", c, "--days", 7)
    else:
        missing.append("Метрика")

    if linked["webmaster"]:
        add("webmaster_queries", "Вебмастер", "yandex.py", "queries", timeout=240)
        add("webmaster_indexing", "Вебмастер", "yandex.py", "indexing")
        add("webmaster_history", "Вебмастер", "yandex.py", "history", "--days", 60)
    else:
        missing.append("Вебмастер")

    if linked["gsc"]:
        add("gsc_summary_28", "Search Console", "gsc.py", "summary", "--days", 28)
        add("gsc_summary_90", "Search Console", "gsc.py", "summary", "--days", 90)
        for c in ("queries", "pages", "devices"):
            add(f"gsc_{c}", "Search Console", "gsc.py", c, "--days", 28)
        add("gsc_sitemaps", "Search Console", "gsc.py", "sitemaps")
        add("gsc_reach_8_20", "Search Console", "gsc_reach.py")
        if pulse:
            add("gsc_summary_7", "Search Console", "gsc.py", "summary", "--days", 7)
            add("gsc_pages_7", "Search Console", "gsc.py", "pages", "--days", 7)
    else:
        missing.append("Search Console")

    if any((_site.CONFIG_DIR / "logs").glob("access-*.log.gz")):
        for c in ("report", "pages", "crawlers", "ai"):
            add(f"logs_{c}", "Логи", "logs.py", c, "--days", 30, timeout=300)
        add("logs_weekly", "Логи", "logs_weekly.py", out / "logs_weekly.json", timeout=300)
        # Периоды 7, 28 и 90 дней против предыдущих: посетители, сдвиги, чтения и переходы нейросетей.
        add("periods", "Логи", "periods.py", out / "periods.json", timeout=300)
    else:
        missing.append("Логи сервера")

    if (_site.CONFIG_DIR / "positions").exists() and any((_site.CONFIG_DIR / "positions").glob("*.json")):
        add("positions", "Позиции", "positions.py", "--show")

    if full and (linked["gsc"] or linked["webmaster"]):
        add("indexation", "Индексация", "indexation.py", timeout=1800)

    if _site.SITE.get("leads_command"):
        j.append({"name": "leads", "group": "Лиды", "timeout": 300, "shell": True,
                  "cmd": _site.SITE["leads_command"]})
    else:
        missing.append("Лиды и продажи")
    return j, missing


# Метрика ограничивает число одновременных запросов на пользователя (ответ 429
# quota_parallel_requests_by_uid), поэтому ее команды идут строго по очереди.
# Временный отказ (429, 503) повторяется с паузой: иначе сторож поднимал бы
# ложную тревогу из-за секундного сбоя.
SERIAL = {"Метрика": threading.Lock(), "Вебмастер": threading.Lock()}
TRANSIENT = ("429:", "503:", "Quota exceeded", "Service unavailable")


def run_once(job):
    try:
        p = subprocess.run(job["cmd"], shell=job.get("shell", False), capture_output=True, text=True,
                           timeout=job["timeout"])
        return p.stdout + (("\n[stderr]\n" + p.stderr) if p.stderr.strip() else ""), p.returncode
    except subprocess.TimeoutExpired:
        return f"timed out after {job['timeout']} s", -1


def run(job, out: Path):
    lock = SERIAL.get(job["group"])
    if lock:
        lock.acquire()
    try:
        for attempt in range(2):
            text, code = run_once(job)
            if not any(m in text for m in TRANSIENT):
                break
            time.sleep(6 * (attempt + 1))
    finally:
        if lock:
            lock.release()
    reason = ""
    if code != 0:
        reason = next((l.strip() for l in reversed(text.splitlines()) if l.strip()), f"код {code}")
    else:
        hit = next((m for m in FAIL_MARKS if m in text), None)
        if hit:
            reason = hit
        elif len(text.strip()) < 20:
            reason = "пустой вывод"
    # Пустой ответ это не ноль: Вебмастер умеет ответить без ошибки пустой таблицей.
    if not reason and job["name"] == "webmaster_queries" and "Всего: запросов 0" in text:
        reason = "Вебмастер ответил, но запросов нет: это отсутствие замера, а не ноль видимости"
    if reason:
        text = f"{NOT_MEASURED}: {reason}. Цифры ниже не использовать.\n\n" + text
    (out / f"{job['name']}.txt").write_text(text, encoding="utf-8")
    return {"name": job["name"], "group": job["group"], "ok": not reason, "reason": reason[:160]}


def report(results, missing):
    groups = {}
    for r in results:
        groups.setdefault(r["group"], []).append(r)
    for g, rs in groups.items():
        bad = [r for r in rs if not r["ok"]]
        state = "работает" if not bad else ("НЕ РАБОТАЕТ" if len(bad) == len(rs) else "частично")
        print(f"  {g:16} {state}")
        for r in bad:
            print(f"      {r['name']}: {r['reason']}")
    for g in missing:
        print(f"  {g:16} не подключен")
    if any(not r["ok"] for r in results):
        print(f"\nУпавшие источники назвать в отчете прямо. Обходные пути: {_site.SKILL_DIR / 'references/sources.md'}")
    if missing:
        print("Неподключенные источники в отчете называются неподключенными: это не ноль.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--force", action="store_true", help="снять заново, даже если свежий снимок есть")
    ap.add_argument("--pulse", action="store_true", help="снимок для пульса, вне рабочей папки")
    args = ap.parse_args()

    out = PULSE_DIR / args.date if args.pulse else _site.WORKDIR / args.date / "raw"
    out.mkdir(parents=True, exist_ok=True)

    old = {}
    mf = out / "manifest.json"
    if mf.exists():
        try:
            old = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            old = {}
    if old and not args.force and not (args.full and not old.get("full")):
        age = (dt.datetime.now() - dt.datetime.fromisoformat(old["made_at"])).total_seconds() / 3600
        if age < REUSE_HOURS:
            print(f"\nСвежий снимок уже есть ({old['made_at']}, {age:.1f} ч назад): {out}")
            print("Беру его. Чтобы снять заново, добавь --force.\n")
            report(old["sources"], old.get("not_connected", []))
            return

    todo, missing = jobs(args.full, args.pulse, out)
    if not todo:
        sys.exit("Ни один источник не подключен: снимать нечего. Начни с онбординга "
                 f"({_site.SKILL_DIR / 'references/onboarding.md'}).")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda job: run(job, out), todo))

    # То, что снято раньше в эту же папку и сейчас не переснималось, остается в манифесте.
    fresh_names = {r["name"] for r in results}
    kept = [r for r in old.get("sources", []) if r["name"] not in fresh_names and (out / f"{r['name']}.txt").exists()]
    results = results + kept
    manifest = {"date": args.date, "site": _site.DOMAIN, "full": args.full or bool(old.get("full")),
                "made_at": dt.datetime.now().isoformat(timespec="seconds"),
                "sources": results, "not_connected": missing}
    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nСнимок {args.date} лежит в {out}\n")
    report(results, missing)
    if args.pulse:
        # Кандидаты для рекомендаций: после всех источников, им нужен periods.json этого же снимка.
        try:
            r = subprocess.run([sys.executable, str(HERE / "opportunities.py"), str(out), "--site", _site.DOMAIN],
                               capture_output=True, text=True, timeout=900)
            text = r.stdout + (("\n[stderr]\n" + r.stderr) if r.returncode else "")
            if r.returncode:
                text = f"{NOT_MEASURED}: кандидаты не посчитались.\n\n" + text
        except Exception as e:  # noqa: BLE001
            text = f"{NOT_MEASURED}: {e}\n"
        (out / "opportunities.txt").write_text(text, encoding="utf-8")
        print(f"\nКандидаты для рекомендаций: {out / 'opportunities.txt'}")


if __name__ == "__main__":
    main()
