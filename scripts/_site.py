"""Настройки сайта и ключи для всех скриптов скилла.

Ничего про конкретный сайт в скриптах не зашито. Все лежит вне папки скилла:

    ~/.config/seo-geo-agent/<домен>/
        site.json                   настройки сайта (не секреты)
        secrets.env                 ключи и токены, права 600
        gsc-service-account.json    ключ сервисного аккаунта Google, права 600
        wordstat/ positions/ pulse/ logs/ watch/    кэш и снимки

Рабочие документы (профиль сайта, замеры, журнал гипотез) лежат в папке
workdir из site.json: их читает и правит человек.

Какой сайт активен: аргумент --site <домен>, затем переменная SEO_SITE, затем
файл ~/.config/seo-geo-agent/current, затем единственный настроенный сайт.
Корень можно перенести переменной SEO_AGENT_HOME.

Если рядом с настройками есть виртуальное окружение (его создает
`setup.py deps`), скрипт сам перезапускается в нем: команда всегда одна и та
же, `python3 scripts/<имя>.py`.
"""
import json
import os
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
BASE = Path(os.environ.get("SEO_AGENT_HOME") or Path.home() / ".config" / "seo-geo-agent").expanduser()
VENV = BASE / "venv"
VENV_PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python3")

# Какие ключи site.json раньше могли лежать в secrets.env под другим именем.
_LEGACY = {"metrika_counter": "METRIKA_COUNTER_ID", "webmaster_host_id": "WEBMASTER_HOST_ID",
           "gsc_site_url": "GSC_SITE_URL"}


def _reexec():
    """Перезапуск в окружении скилла, если оно есть и мы еще не в нем."""
    if os.environ.get("SEO_AGENT_NO_VENV") or not VENV_PY.exists():
        return
    try:
        if Path(sys.prefix).resolve() == VENV.resolve():
            return
    except OSError:
        return
    os.environ["SEO_AGENT_NO_VENV"] = "1"       # защита от петли
    os.execv(str(VENV_PY), [str(VENV_PY)] + sys.argv)


_reexec()


def sites() -> list[str]:
    if not BASE.exists():
        return []
    return sorted(d.name for d in BASE.iterdir() if (d / "site.json").exists())


def _take_site_arg() -> str | None:
    """Забрать --site <домен> из аргументов сразу при импорте, пока скрипт их не разобрал."""
    if "--site" in sys.argv:
        i = sys.argv.index("--site")
        if i + 1 < len(sys.argv):
            name = sys.argv[i + 1]
            del sys.argv[i:i + 2]
            return name
    return None


_SITE_ARG = _take_site_arg()


def _pick() -> str:
    name = _SITE_ARG or os.environ.get("SEO_SITE")
    if not name and (BASE / "current").exists():
        name = (BASE / "current").read_text(encoding="utf-8").strip()
    have = sites()
    if not name:
        if len(have) == 1:
            name = have[0]
        elif not have:
            sys.exit("Сайт еще не настроен. Начни с онбординга: "
                     f"python3 {SKILL_DIR / 'scripts/setup.py'} init <домен>")
        else:
            sys.exit(f"Настроено несколько сайтов ({', '.join(have)}). Укажи --site <домен> "
                     "или выбери основной: setup.py use <домен>")
    if name not in have:
        sys.exit(f"Сайт {name} не настроен. Есть: {', '.join(have) or 'ни одного'}. "
                 "Создать: setup.py init <домен>")
    return name


_cache: dict = {}


def _load() -> dict:
    if not _cache:
        name = _pick()
        folder = BASE / name
        site = json.loads((folder / "site.json").read_text(encoding="utf-8"))
        site.setdefault("domain", name)
        _cache.update(SITE=site, CONFIG_DIR=folder, DOMAIN=site["domain"],
                      WORKDIR=Path(site.get("workdir") or folder / "work").expanduser())
    return _cache


def __getattr__(name):
    """SITE, CONFIG_DIR, WORKDIR и DOMAIN считаются при первом обращении."""
    if name in ("SITE", "CONFIG_DIR", "WORKDIR", "DOMAIN"):
        return _load()[name]
    raise AttributeError(name)


def site_url() -> str:
    s = _load()["SITE"]
    return (s.get("url") or f"https://{s['domain']}").rstrip("/")


def secrets_file() -> Path:
    return _load()["CONFIG_DIR"] / "secrets.env"


def secrets() -> dict:
    """Ключи из secrets.env. Переменные окружения с тем же именем важнее."""
    values = {}
    f = secrets_file()
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key in list(values):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def secret(key: str, required: bool = True) -> str | None:
    value = secrets().get(key)
    if not value and required:
        sys.exit(f"В {secrets_file()} не заполнен {key}. Как получить: "
                 f"{SKILL_DIR / 'references/onboarding.md'}")
    return value or None


def setting(key: str, default=None):
    """Значение из site.json; для идентификаторов есть запасной путь через secrets.env."""
    value = _load()["SITE"].get(key)
    if value in (None, "") and key in _LEGACY:
        value = secrets().get(_LEGACY[key])
    return default if value in (None, "") else value


def save_setting(key: str, value) -> None:
    data = _load()
    data["SITE"][key] = value
    (data["CONFIG_DIR"] / "site.json").write_text(
        json.dumps(data["SITE"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def gsc_key_file() -> Path:
    custom = secrets().get("GSC_SERVICE_ACCOUNT_JSON")
    return Path(custom).expanduser() if custom else _load()["CONFIG_DIR"] / "gsc-service-account.json"


def connected() -> dict:
    """Какие источники подключены. Значения ключей не читаются наружу, только факт наличия."""
    keys = secrets()
    cfg = _load()["CONFIG_DIR"]
    logs = (_load()["SITE"].get("logs") or {})
    return {
        "metrika": bool(keys.get("YANDEX_OAUTH_TOKEN") and setting("metrika_counter")),
        "webmaster": bool(keys.get("YANDEX_OAUTH_TOKEN") and setting("webmaster_host_id")),
        "yandex_token": bool(keys.get("YANDEX_OAUTH_TOKEN")),
        "gsc": gsc_key_file().exists() and bool(setting("gsc_site_url")),
        "gsc_key": gsc_key_file().exists(),
        "wordstat": bool(keys.get("YANDEX_SEARCH_API_KEY") and keys.get("YANDEX_FOLDER_ID")),
        "logs": any((cfg / "logs").glob("access-*.log.gz")) or logs.get("source", "none") != "none",
        "leads": bool(_load()["SITE"].get("leads_command")),
    }
