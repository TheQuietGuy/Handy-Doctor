"""scripts/ingest/parser.py -- HTML to clean text."""

from __future__ import annotations

import re
import unicodedata

try:
    from bs4 import BeautifulSoup

    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False

_NOISE = [
    "back to top",
    "page last reviewed",
    "next review due",
    "help us improve",
    "share this page",
    "print this page",
    "was this page helpful",
    "cookie policy",
]
_SKIP_TAGS = {
    "script",
    "style",
    "svg",
    "nav",
    "header",
    "footer",
    "aside",
    "noscript",
    "iframe",
    "form",
    "button",
}
_SELECTORS = [
    'article[role="main"]',
    'main[id="main-content"]',
    "main",
    "article",
    '[role="main"]',
    "#content",
    ".content",
    "body",
]


def parse(html_bytes: bytes, url: str = "") -> str:
    if _HAS_BS4:
        soup = BeautifulSoup(html_bytes, "html.parser")
        for t in _SKIP_TAGS:
            for el in soup.find_all(t):
                el.decompose()
        for sel in [
            '[class*="cookie"]',
            '[class*="breadcrumb"]',
            '[class*="feedback"]',
            '[class*="share"]',
        ]:
            for el in soup.select(sel):
                el.decompose()
        content = None
        for sel in _SELECTORS:
            content = soup.select_one(sel)
            if content:
                break
        raw = (content or soup.body or soup).get_text(separator="\n")
    else:
        text = html_bytes.decode("utf-8", errors="replace")
        text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        raw = re.sub(r"&[a-z]+;", " ", text)
    return _clean(raw)


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]", "", text)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line:
            continue
        if any(p in line.lower() for p in _NOISE):
            continue
        if len(line) < 20 and not re.search(r"[.!?]", line):
            continue
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
