"""Тексты страниц сайта для проверок, которым нужно читать содержимое.

Два источника, по порядку:
  1. Папка с исходниками сайта (`site_dir` в site.json): репозиторий, выгрузка
     статического сайта, папка темы. Читается с диска, сеть не нужна, видна и
     разметка, и черновики, которые еще не выложены.
  2. Живой сайт: адреса из карты сайта (sitemap.xml или карты из robots.txt) и llms.txt.

Сайт на конструкторе или в CMS без выгрузки проверяется вторым способом.
"""
import re
import subprocess
from pathlib import Path

import _site
from sitecheck import get, sitemap_urls

SUFFIXES = {".html", ".htm", ".xml"}
SKIP = {".git", "node_modules", "vendor", ".venv", "venv", "dist-cache", "__pycache__"}


def source_dir() -> Path | None:
    value = _site.setting("site_dir")
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.exists() else None


def _local_files(root: Path) -> list[Path]:
    skip_extra = set(_site.SITE.get("site_dir_skip", []))
    try:
        names = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True,
                               text=True, check=True).stdout.splitlines()
        files = [root / n for n in names]
    except (subprocess.CalledProcessError, FileNotFoundError):
        files = [p for p in root.rglob("*") if p.is_file()]
    out = []
    for f in files:
        rel = f.relative_to(root)
        if set(rel.parts) & SKIP or (rel.parts and rel.parts[0] in skip_extra):
            continue
        if f.suffix.lower() in SUFFIXES or f.name == "llms.txt":
            out.append(f)
    return out


def _live_urls(max_pages: int) -> list[str]:
    base = _site.site_url()
    urls = sitemap_urls(base + "/sitemap.xml")
    if not urls:
        robots = get(base + "/robots.txt")
        if not isinstance(robots, Exception) and robots.status_code == 200:
            for sm in re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots.text):
                urls += sitemap_urls(sm)
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:max_pages] + [base + "/llms.txt"]


def iter_pages(max_pages: int = 300):
    """Пары (метка, текст). Метка это путь файла или адрес страницы."""
    root = source_dir()
    if root:
        for f in _local_files(root):
            try:
                yield str(f.relative_to(root)), f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        return
    for url in _live_urls(max_pages):
        r = get(url)
        if isinstance(r, Exception) or r.status_code != 200:
            continue
        rest = url.split("://", 1)[-1]
        yield ("/" + rest.split("/", 1)[1] if "/" in rest else "/"), r.text


def describe() -> str:
    root = source_dir()
    return f"папка исходников {root}" if root else f"живой сайт {_site.site_url()} по карте сайта"


def page_html(path: str) -> tuple[str, str]:
    """HTML одной страницы: (откуда взят, текст). Сначала исходники, потом живой сайт."""
    root = source_dir()
    if root:
        clean = path.strip("/")
        for cand in (root / clean / "index.html", root / f"{clean or 'index'}.html", root / clean):
            if cand.is_file():
                return f"файл {cand}", cand.read_text(encoding="utf-8", errors="replace")
    url = _site.site_url() + (path if path.startswith("/") else "/" + path)
    r = get(url)
    if isinstance(r, Exception) or r.status_code != 200:
        raise SystemExit(f"Страница не открылась: {url} ({r})")
    return f"сайт {url}", r.text
