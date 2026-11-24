#!/usr/bin/env python3
"""Access-логи сервера: живые посетители, источники, AI-краулеры и переходы из нейросетей.

Необязательный источник, но самый честный: счетчики видят не всех (блокировщики,
согласие на куки), а переходы из нейросетей и обходы AI-краулеров кроме логов
не видит никто. Нужен доступ к access-логам в формате combined (nginx и Apache
пишут его по умолчанию). На конструкторах сайтов логов обычно нет: тогда этот
источник просто не подключается.

Хостинги хранят логи недолго (часто 7-14 суток), поэтому скрипт копит архив у
себя: по одному gzip-файлу на сутки в <настройки сайта>/logs/. Запускать sync
хотя бы раз в неделю, лучше каждый день по расписанию.

Откуда брать логи, задается в site.json, ключ logs:
    {"source": "dir", "path": "/var/log/nginx", "pattern": "access.log*"}
    {"source": "ftp", "host": "ftp.example.ru", "remote_dir": "/logs", "pattern": "access"}
        логин и пароль: LOGS_FTP_USER и LOGS_FTP_PASSWORD в secrets.env
    {"source": "command", "command": "rsync -az user@host:/var/log/nginx/access.log* {inbox}/"}
        любая команда, которая кладет файлы логов в папку {inbox}

Команды:
    logs.py sync                забрать новые логи в архив
    logs.py import <файл...>    добавить в архив файлы логов, скачанные руками
    logs.py report [--days N]   посетители и источники по дням
    logs.py pages  [--days N]   страницы входа
    logs.py crawlers [--days N] обходы AI-краулеров и поисковых ботов, их любимые страницы
    logs.py ai     [--days N]   живые переходы из AI-интерфейсов по рефереру и по utm_source

Как считается живой посетитель: первое касание за сутки по IP, без ботов, и только
если с этого IP в те же сутки загружалась статика сайта (картинки, стили, скрипты).
Боты статику обычно не грузят. Если статика сайта лежит на другом домене или CDN,
поставь в site.json "logs": {"require_assets": false}, иначе посетителей будет ноль.
"""

import argparse
import ftplib
import gzip
import io
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _site  # noqa: E402

ARCHIVE = _site.CONFIG_DIR / "logs"
CFG = _site.SITE.get("logs") or {}
REQUIRE_ASSETS = CFG.get("require_assets", True)
OWN = _site.DOMAIN.lower()

LINE = re.compile(
    r'^(\S+) \S+ \S+ \[(\d{2})/(\w{3})/(\d{4}):\d{2}:\d{2}:\d{2}[^\]]*\] '
    r'"\S+ ([^"]*?) \S+" (\d{3}) \S+ "([^"]*)" "([^"]*)"'
)
BOT = re.compile(
    r'bot|crawler|spider|slurp|yandex|google|bing|ahrefs|semrush|mj12|dotbot|petal'
    r'|facebook|telegram|headless|python|curl|wget|scrapy|go-http|java/|okhttp|monitor|uptime',
    re.I,
)
ASSET = re.compile(r'\.(css|js|png|jpe?g|gif|webp|avif|svg|ico|woff2?|ttf|xml|txt|map|pdf|mp4|webm)(\?|$)', re.I)
BROWSER_ASSET = re.compile(r'\.(css|js|webp|avif|png|jpe?g|gif|svg|woff2?)(\?|$)', re.I)
MONTHS = {m: f"{i:02d}" for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}

# Названия групп источников. Свои группы добавляются в site.json, ключ referrer_groups:
# {"Маркетплейс": ["ozon.ru", "wildberries.ru"]}.
DIRECT, SEARCH, AI, SOCIAL, OTHER = "Прямые и мессенджеры", "Поиск", "AI-ответы", "Соцсети", "Другие сайты"
CUSTOM = {name: tuple(h.lower() for h in hosts) for name, hosts in (_site.SITE.get("referrer_groups") or {}).items()}
SEARCH_HOSTS = ("google.", "yandex.", "ya.ru", "bing.com", "duckduckgo.", "go.mail.ru", "yahoo.",
                "nova.rambler.ru", "search.brave.com", "ecosia.org", "startpage.com", "baidu.com")
NOT_SEARCH = ("mail.google.", "mail.yandex.", "docs.google.", "translate.", "maps.", "disk.yandex.", "dzen.")
SOCIAL_HOSTS = ("vk.com", "vk.ru", "t.me", "telegram.", "instagram.", "threads.", "facebook.", "ok.ru",
                "youtube.", "youtu.be", "dzen.ru", "x.com", "twitter.", "t.co", "linkedin.", "tiktok.",
                "pinterest.", "reddit.", "pikabu.ru", "rutube.ru")

# Краулеры, по которым смотрим GEO. Порядок это порядок в отчете.
# «Пользовательские» агенты (ChatGPT-User, Claude-User, Perplexity-User) срабатывают
# и когда человек сам вставил ссылку в чат, поэтому это верхняя оценка цитирования.
AI_CRAWLERS = [
    "GPTBot", "OAI-SearchBot", "ChatGPT-User",
    "ClaudeBot", "Claude-User", "Claude-SearchBot",
    "PerplexityBot", "Perplexity-User",
    "Google-Extended", "GoogleOther", "DuckAssistBot", "Applebot",
    "meta-externalagent", "Bytespider", "CCBot", "Amazonbot", "YandexAdditional",
]
SEARCH_CRAWLERS = ["YandexBot", "Googlebot", "bingbot"]

# Хосты AI-интерфейсов, из которых приходят живые люди по клику на источник.
AI_REFERERS = (
    "chatgpt.com", "chat.openai.com", "perplexity.ai", "claude.ai",
    "gemini.google.com", "copilot.microsoft.com", "you.com", "phind.com",
    "alice.yandex.ru", "ya.ru/alice", "search.yandex.ru/neuro", "duckduckgo.com/aichat",
    "mistral.ai", "chat.deepseek.com", "grok.com", "x.ai", "giga.chat",
)


# ---------------------------------------------------------------- сбор архива

def day_of(line: str) -> str | None:
    m = LINE.match(line)
    if not m:
        return None
    _, dd, mon, yy = m.groups()[:4]
    return f"{yy}-{MONTHS.get(mon, '00')}-{dd}"


def absorb(name: str, raw: bytes) -> tuple[int, int]:
    """Разложить файл лога по суткам и долить в архив. Возвращает (новых строк, неразобранных, разобранных)."""
    if name.endswith(".gz") or raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            print(f"  {name}: битый архив, пропускаю")
            return 0, 0, 0
    by_day, bad = defaultdict(list), 0
    for line in raw.decode("utf-8", "replace").splitlines():
        day = day_of(line)
        if day:
            by_day[day].append(line)
        elif line.strip():
            bad += 1
    added = 0
    for day, new in by_day.items():
        target = ARCHIVE / f"access-{day}.log.gz"
        old = gzip.decompress(target.read_bytes()).decode("utf-8", "replace").splitlines() if target.exists() else []
        have = set(old)
        fresh = [l for l in dict.fromkeys(new) if l not in have]
        if fresh:
            target.write_bytes(gzip.compress("\n".join(old + fresh).encode("utf-8")))
            added += len(fresh)
    return added, bad, sum(len(v) for v in by_day.values())


def from_dir(folder: Path, pattern: str):
    for path in sorted(folder.glob(pattern)):
        if path.is_file():
            yield path.name, path.read_bytes()


def from_ftp():
    host = CFG.get("host")
    if not host:
        sys.exit('В site.json у источника ftp не указан host: "logs": {"source": "ftp", "host": "..."}')
    user, password = _site.secret("LOGS_FTP_USER"), _site.secret("LOGS_FTP_PASSWORD")
    ftp = ftplib.FTP_TLS(timeout=60) if CFG.get("tls", True) else ftplib.FTP(timeout=60)
    ftp.connect(host, int(CFG.get("port", 21)))
    ftp.login(user, password)
    if CFG.get("tls", True):
        ftp.prot_p()
    ftp.cwd(CFG.get("remote_dir", "/"))
    want = CFG.get("pattern", "access")
    for name in sorted(n for n in ftp.nlst() if want in n):
        buf = io.BytesIO()
        ftp.retrbinary(f"RETR {name}", buf.write)
        yield name, buf.getvalue()
    ftp.quit()


def ingest(files) -> None:
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    total = bad_total = parsed = n = 0
    for name, raw in files:
        added, bad, good = absorb(name, raw)
        n += 1
        total += added
        bad_total += bad
        parsed += good
        print(f"  {name}: новых строк {added}" + (f", не разобрано {bad}" if bad else ""))
    days = sorted(ARCHIVE.glob("access-*.log.gz"))
    print(f"\nфайлов просмотрено: {n}, новых строк: {total}")
    if bad_total > parsed:
        print("⚠ Большая часть строк не разобрана: похоже, формат лога не combined. "
              "Покажи владельцу или хостингу пример строки и попроси формат combined.")
    if days:
        print(f"в архиве {len(days)} суток: {days[0].name[7:17]} .. {days[-1].name[7:17]}")


def cmd_sync(args):
    source = CFG.get("source", "none")
    if source == "dir":
        folder = Path(CFG.get("path", "")).expanduser()
        if not folder.is_dir():
            sys.exit(f"Папки с логами нет: {folder}")
        ingest(from_dir(folder, CFG.get("pattern", "*access*log*")))
    elif source == "ftp":
        ingest(from_ftp())
    elif source == "command":
        inbox = ARCHIVE / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        command = CFG.get("command", "").replace("{inbox}", str(inbox))
        if not command:
            sys.exit('В site.json у источника command не указана команда: "logs": {"source": "command", "command": "..."}')
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            sys.exit(f"Команда забора логов завершилась с ошибкой {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}")
        ingest(from_dir(inbox, "*"))
        for f in inbox.iterdir():
            if f.is_file():
                f.unlink()
    else:
        sys.exit("Источник логов не настроен. Варианты: references/onboarding.md, раздел про логи. "
                 "Разово можно добавить файлы руками: logs.py import <файл>")


def cmd_import(args):
    if not args.files:
        sys.exit("Укажи файлы логов: logs.py import access.log access.log.1.gz")
    ingest((Path(f).name, Path(f).expanduser().read_bytes()) for f in args.files)


# ---------------------------------------------------------------- разбор

def lines(days: int | None):
    """Разобранные строки архива без дублей: (день, ip, url, код, реферер, ua)."""
    files = sorted(ARCHIVE.glob("access-*.log.gz"))
    if not files:
        sys.exit("Архив логов пуст. Сначала: logs.py sync (или logs.py import <файл>)")
    if days:
        files = files[-days:]

    seen = set()
    for path in files:
        for line in gzip.decompress(path.read_bytes()).decode("utf-8", "replace").splitlines():
            if line in seen:
                continue
            seen.add(line)
            m = LINE.match(line)
            if not m:
                continue
            ip, dd, mon, yy, url, code, ref, ua = m.groups()
            day = f"{yy}-{MONTHS.get(mon, '00')}-{dd}"
            yield day, ip, url, code, ref, ua


class Browsers(defaultdict):
    """Сутки -> IP, загружавшие статику. При require_assets=false живым считается любой IP."""

    def __init__(self):
        super().__init__(lambda: _Everyone() if not REQUIRE_ASSETS else set())


class _Everyone(set):
    def __contains__(self, item):
        return True


def load(days: int | None):
    events = []
    browser = Browsers()
    for day, ip, url, code, ref, ua in lines(days):
        if ASSET.search(url):
            if BROWSER_ASSET.search(url):
                browser[day].add(ip)
            continue
        if BOT.search(ua) or not code.startswith(("2", "3")):
            continue
        events.append((day, ip, url, ref))
    return events, browser


def source(ref: str) -> str | None:
    """Группа источника по рефереру. None значит переход внутри самого сайта."""
    if not ref or ref == "-":
        return DIRECT
    m = re.match(r"https?://([^/]+)", ref)
    host = m.group(1).lower().removeprefix("www.") if m else ""
    if not host:
        return OTHER
    if host == OWN or host.endswith("." + OWN):
        return None
    if ai_referer(ref):
        return AI
    for name, hosts in CUSTOM.items():
        if any(h in host for h in hosts):
            return name
    if any(s in host for s in SEARCH_HOSTS) and not any(host.startswith(x) for x in NOT_SEARCH):
        return SEARCH
    if any(s in host for s in SOCIAL_HOSTS):
        return SOCIAL
    return OTHER


def groups() -> list[str]:
    """Порядок групп в отчетах."""
    return [DIRECT, SEARCH, AI, SOCIAL, *CUSTOM, OTHER]


def ai_referer(ref: str) -> str | None:
    """Хост AI-интерфейса из реферера, если переход оттуда."""
    if not ref or ref == "-":
        return None
    low = ref.lower().replace("://www.", "://")
    for host in AI_REFERERS:
        if "://" + host in low:
            return host
    return None


# Приложения ChatGPT, Copilot и Perplexity часто не передают реферер, зато дописывают
# к ссылке utm_source. Без этой метки большая часть переходов из ChatGPT лежит в «прямых».
AI_UTM = re.compile(
    r"[?&]utm_source=(chatgpt\.com|chatgpt|openai|perplexity(?:\.ai)?|copilot(?:\.com)?"
    r"|claude(?:\.ai)?|gemini|deepseek|grok|alice|you\.com|phind|mistral|giga(?:chat)?)(?:&|$)",
    re.I,
)
UTM_HOST = {"chatgpt": "chatgpt.com", "openai": "chatgpt.com", "perplexity": "perplexity.ai",
            "copilot": "copilot.microsoft.com", "copilot.com": "copilot.microsoft.com",
            "claude": "claude.ai", "alice": "alice.yandex.ru"}


def ai_origin(ref: str, url: str) -> str | None:
    """AI-интерфейс, из которого пришел человек: по рефереру или по utm_source."""
    host = ai_referer(ref)
    if host:
        return host
    m = AI_UTM.search(url or "")
    if not m:
        return None
    value = m.group(1).lower()
    return UTM_HOST.get(value, value)


def visitors(events, browser):
    """Первое касание за сутки: каждый посетитель считается один раз."""
    first, landing = {}, {}
    for day, ip, url, ref in events:
        if ip not in browser[day]:
            continue
        key = (day, ip)
        if key in first:
            continue
        first[key] = AI if ai_origin(ref, url) else (source(ref) or DIRECT)
        landing[key] = url
    return first, landing


def hint_no_assets(events, first):
    if events and not first and REQUIRE_ASSETS:
        print("\n⚠ Запросы страниц в логах есть, а посетителей ноль: похоже, статика сайта отдается "
              'с другого домена или CDN. Поставь в site.json "logs": {"require_assets": false}.')


# ---------------------------------------------------------------- отчеты

def cmd_report(args):
    events, browser = load(args.days)
    first, _ = visitors(events, browser)

    by_day = defaultdict(Counter)
    for (day, _ip), src in first.items():
        by_day[day][src] += 1

    totals = Counter(first.values())
    cols = [g for g in groups() if totals[g]]
    print(f"\nЖивые посетители по логам сервера, всего {len(first)}\n")
    print("  дата        всего " + "".join(c[:20].rjust(22) for c in cols))
    print("  " + "-" * (18 + 22 * len(cols)))
    for day in sorted(by_day):
        row = by_day[day]
        print(f"  {day}  {sum(row.values()):5} " +
              "".join(str(row.get(c, 0)).rjust(22) for c in cols))

    print("\n  Доли за период")
    total = len(first) or 1
    for src, n in totals.most_common():
        print(f"    {n:6}  {n * 100 // total:3}%  {src}")
    print(f"\n  «{DIRECT}» это заходы без реферера: закладки, мессенджеры, приложения, часть переходов из нейросетей.")
    hint_no_assets(events, first)


def cmd_pages(args):
    events, browser = load(args.days)
    first, landing = visitors(events, browser)
    print(f"\nСтраницы входа, всего посетителей {len(landing)}\n")
    for url, n in Counter(u.split("?")[0] for u in landing.values()).most_common(30):
        print(f"  {n:6}  {url[:66]}")
    hint_no_assets(events, first)


def crawler_name(ua: str) -> str | None:
    """Имя краулера из UA. GoogleOther и Google-Extended стоят в списке раньше Googlebot."""
    low = ua.lower()
    for name in AI_CRAWLERS + SEARCH_CRAWLERS:
        if name.lower() in low:
            return name
    return None


def cmd_crawlers(args):
    hits = Counter()
    pages = defaultdict(Counter)
    days = set()
    for day, _ip, url, code, _ref, ua in lines(args.days):
        name = crawler_name(ua)
        if not name:
            continue
        days.add(day)
        hits[name] += 1
        if not ASSET.search(url) and code.startswith("2"):
            pages[name][url.split("?")[0]] += 1

    span = f"{min(days)} .. {max(days)}" if days else "нет данных"
    print(f"\nОбходы краулеров за {len(days)} суток ({span})\n")
    print("  AI-краулеры (запросов / из них страниц с ответом 200)")
    for name in AI_CRAWLERS:
        if hits[name]:
            print(f"    {hits[name]:6}  {sum(pages[name].values()):6}  {name}")
    if not any(hits[n] for n in AI_CRAWLERS):
        print("    ни одного: проверь, не закрыты ли они в robots.txt (sitecheck.py)")
    print("\n  Поисковые, для масштаба")
    for name in SEARCH_CRAWLERS:
        print(f"    {hits[name]:6}  {sum(pages[name].values()):6}  {name}")

    ai_pages = Counter()
    for name in AI_CRAWLERS:
        ai_pages.update(pages[name])
    print("\n  Что чаще всего читают AI-краулеры (все вместе)")
    for url, n in ai_pages.most_common(15):
        print(f"    {n:6}  {url[:66]}")

    print("\n  Оговорка: ChatGPT-User, Claude-User и Perplexity-User ходят и по ссылке,")
    print("  которую человек сам вставил в чат. Чистое цитирование они не измеряют.")


def cmd_ai(args):
    events, browser = load(args.days)
    days = {day for day, *_ in events}
    first, landing, hosts = {}, {}, {}
    for day, ip, url, ref in events:
        host = ai_origin(ref, url)
        if not host or ip not in browser[day]:
            continue
        key = (day, ip)
        if key in first:
            continue
        first[key] = day
        landing[key] = url.split("?")[0]
        hosts[key] = host

    search = sum(1 for (_d, _ip), src in visitors(events, browser)[0].items() if src == SEARCH)
    span = f"{min(days)} .. {max(days)}" if days else "нет данных"
    print(f"\nЖивые переходы из AI-интерфейсов за {len(days)} суток ({span}): {len(first)}")
    print(f"для масштаба, из обычного поиска за то же время: {search}\n")
    if not first:
        return
    print("  Откуда")
    for host, n in Counter(hosts.values()).most_common():
        print(f"    {n:6}  {host}")
    print("\n  Куда пришли")
    for url, n in Counter(landing.values()).most_common(15):
        print(f"    {n:6}  {url[:66]}")
    print("\n  По дням")
    for day, n in sorted(Counter(first.values()).items()):
        print(f"    {day}  {n}")


COMMANDS = {"sync": cmd_sync, "import": cmd_import, "report": cmd_report, "pages": cmd_pages,
            "crawlers": cmd_crawlers, "ai": cmd_ai}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("files", nargs="*")
    parser.add_argument("--days", type=int, default=None)
    args = parser.parse_args()
    COMMANDS[args.command](args)
    print()


if __name__ == "__main__":
    main()
