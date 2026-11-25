#!/usr/bin/env python3
"""Первая настройка скилла под сайт. Только стандартная библиотека.

    setup.py init <домен> [--workdir <папка>]   создать настройки и рабочие документы сайта
    setup.py deps                               поставить зависимости в окружение скилла
    setup.py secrets                            открыть файл ключей в редакторе
    setup.py status                             что подключено, что нет, что делать дальше
    setup.py use <домен>                        выбрать основной сайт, если их несколько
    setup.py list                               настроенные сайты

Ключи и токены в файл вписывает владелец сам. Скрипт их не печатает: `status`
показывает только, заполнен ключ или нет.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("SEO_AGENT_NO_VENV", "1")   # этому скрипту окружение не нужно
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _site  # noqa: E402

TEMPLATES = _site.SKILL_DIR / "templates"
# Рабочие документы: шаблон -> имя в workdir
DOCS = {"profile.md": "profile.md", "status.md": "status.md", "hypotheses.md": "hypotheses.md",
        "events.md": "events.md", "mistakes.md": "mistakes.md", "queries.txt": "queries.txt",
        "candidates.txt": "candidates.txt", "facts.md": "facts.md"}


def clean_domain(raw: str) -> str:
    d = raw.strip().lower()
    for prefix in ("https://", "http://"):
        d = d.removeprefix(prefix)
    d = d.split("/")[0].removeprefix("www.")
    if not d or "." not in d or " " in d:
        sys.exit(f"Не похоже на домен: {raw!r}. Пример: example.ru")
    return d


def cmd_init(args):
    domain = clean_domain(args.domain)
    folder = _site.BASE / domain
    workdir = Path(args.workdir).expanduser().resolve() if args.workdir else folder / "work"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        folder.chmod(0o700)
    except OSError:
        pass

    site_file = folder / "site.json"
    if site_file.exists():
        print(f"Настройки уже есть: {site_file} (не трогаю)")
    else:
        site = json.loads((TEMPLATES / "site.json").read_text(encoding="utf-8"))
        site["domain"] = domain
        site["workdir"] = str(workdir)
        site_file.write_text(json.dumps(site, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Создал настройки: {site_file}")

    secrets = folder / "secrets.env"
    if secrets.exists():
        print(f"Файл ключей уже есть: {secrets} (не трогаю)")
    else:
        shutil.copy(TEMPLATES / "secrets.env", secrets)
        print(f"Создал файл ключей: {secrets}")
    try:
        secrets.chmod(0o600)
    except OSError:
        pass

    workdir = Path(json.loads(site_file.read_text(encoding="utf-8")).get("workdir") or workdir).expanduser()
    (workdir / "briefs").mkdir(parents=True, exist_ok=True)
    for template, name in DOCS.items():
        target = workdir / name
        if not target.exists():
            text = (TEMPLATES / template).read_text(encoding="utf-8").replace("{{domain}}", domain)
            target.write_text(text, encoding="utf-8")
    print(f"Рабочие документы: {workdir}")

    if not (_site.BASE / "current").exists() or len(_site.sites()) == 1:
        (_site.BASE / "current").write_text(domain + "\n", encoding="utf-8")
    print("\nДальше: setup.py deps, затем подключение источников по references/onboarding.md")


def cmd_deps(args):
    req = _site.SKILL_DIR / "requirements.txt"
    if not _site.VENV_PY.exists():
        _site.BASE.mkdir(parents=True, exist_ok=True)
        print(f"Создаю окружение: {_site.VENV}")
        subprocess.run([sys.executable, "-m", "venv", str(_site.VENV)], check=True)
    r = subprocess.run([str(_site.VENV_PY), "-m", "pip", "install", "--quiet", "--upgrade", "-r", str(req)])
    if r.returncode != 0:
        sys.exit("Зависимости не поставились. Проверь интернет и версию Python (нужен 3.10 или новее).")
    print("Зависимости на месте. Скрипты сами запускаются в этом окружении.")


def cmd_secrets(args):
    path = _site.secrets_file()
    if not path.exists():
        sys.exit(f"Файла ключей нет: {path}. Сначала setup.py init <домен>.")
    print(f"Файл ключей: {path}")
    print("Впиши значения после знака =, сохрани и закрой. В чат ключи не присылай.")
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", "-e", str(path)], check=False)
        elif os.name == "nt":
            subprocess.Popen(["notepad", str(path)])
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        print("Редактор сам не открылся: открой файл руками.")


def cmd_status(args):
    site, cfg = _site.SITE, _site.CONFIG_DIR
    c = _site.connected()
    print(f"\nСайт: {site['domain']}")
    print(f"Настройки: {cfg}")
    print(f"Рабочие документы: {_site.WORKDIR}")
    try:
        import importlib.util
        py = _site.VENV_PY if _site.VENV_PY.exists() else sys.executable
        ok = subprocess.run([str(py), "-c", "import requests, googleapiclient, google.oauth2"],
                            capture_output=True).returncode == 0
    except OSError:
        ok = False
    print(f"Зависимости Python: {'на месте' if ok else 'НЕ поставлены, запусти setup.py deps'}")

    rows = [
        ("Яндекс.Метрика", c["metrika"],
         "токен есть, счетчик не выбран: yandex.py check" if c["yandex_token"] else "нужен YANDEX_OAUTH_TOKEN"),
        ("Яндекс.Вебмастер", c["webmaster"],
         "токен есть, сайт не найден: yandex.py check" if c["yandex_token"] else "нужен YANDEX_OAUTH_TOKEN"),
        ("Google Search Console", c["gsc"],
         "ключ есть, ресурс не выбран: gsc.py check" if c["gsc_key"] else "нужен файл gsc-service-account.json"),
        ("Wordstat и выдача Яндекса", c["wordstat"], "нужны YANDEX_SEARCH_API_KEY и YANDEX_FOLDER_ID"),
        ("Логи сервера (необязательно)", c["logs"], "не настроены: раздел про логи в onboarding.md"),
        ("Лиды и продажи (необязательно)", c["leads"], "не настроены: ключ leads_command в site.json"),
    ]
    print("\nИсточники")
    for name, ok, hint in rows:
        print(f"  {'подключен   ' if ok else 'НЕ подключен'}  {name}" + ("" if ok else f"   [{hint}]"))

    work = _site.WORKDIR
    profile = work / "profile.md"
    filled = profile.exists() and "ЗАПОЛНИТЬ" not in profile.read_text(encoding="utf-8")
    print(f"\nПрофиль сайта: {'заполнен' if filled else 'не заполнен, нужно интервью с владельцем'} ({profile})")
    n = len([f for f in (cfg / "pulse").glob("*/manifest.json")]) if (cfg / "pulse").exists() else 0
    reports = len(list(work.glob("*/report.md")))
    print(f"Снимков пульса: {n}, замеров: {reports}")
    if not any((c["metrika"], c["webmaster"], c["gsc"], c["wordstat"])):
        print("\nНи один источник не подключен: это первый запуск, иди по references/onboarding.md.")
    print()


def cmd_use(args):
    domain = clean_domain(args.domain)
    if domain not in _site.sites():
        sys.exit(f"Сайт {domain} не настроен. Есть: {', '.join(_site.sites()) or 'ни одного'}")
    (_site.BASE / "current").write_text(domain + "\n", encoding="utf-8")
    print(f"Основной сайт: {domain}")


def cmd_list(args):
    cur = (_site.BASE / "current").read_text(encoding="utf-8").strip() if (_site.BASE / "current").exists() else ""
    have = _site.sites()
    if not have:
        print("Сайтов нет. Создать: setup.py init <домен>")
    for d in have:
        print(f"  {'*' if d == cur else ' '} {d}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("domain"); p.add_argument("--workdir"); p.set_defaults(fn=cmd_init)
    sub.add_parser("deps").set_defaults(fn=cmd_deps)
    sub.add_parser("secrets").set_defaults(fn=cmd_secrets)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("use"); p.add_argument("domain"); p.set_defaults(fn=cmd_use)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
