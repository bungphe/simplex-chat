"""Interface languages: the admin UI, staff chat commands, the web shop, receipts, emails.

The texts are written in Vietnamese in the code; `tr()` looks a text up in the catalog of
the current language (`locales/<code>.json`: Vietnamese source -> translation) and fills in
its values. The current language is a context variable, set where a request comes in
(the staff member's choice in the admin UI or their linked chat, the shop's visitor, the
shop's own language for receipts and emails), so texts made anywhere below, including
error messages raised deep in the inventory, come out in that language. Unknown texts
and languages fall back to the Vietnamese source.

Placeholders are positional, as in str.format: `tr("Còn {0} sản phẩm", n)`.
`scripts/i18n_extract.py` collects every Vietnamese text of the code and the admin UI
into the catalogs (new texts are added untranslated; `locales/source.json` lists them).
"""

from __future__ import annotations

import contextvars
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from importlib import resources
from typing import Any

log = logging.getLogger(__name__)

SOURCE = "vi"
# the interface languages: code -> native name (the admin UI's language menu)
LANGUAGES: dict[str, str] = {
    "vi": "Tiếng Việt",
    "en": "English",
    "zh": "中文",
    "ja": "日本語",
    "ko": "한국어",
    "th": "ไทย",
    "id": "Bahasa Indonesia",
    "ms": "Bahasa Melayu",
    "km": "ខ្មែរ",
    "lo": "ລາວ",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "pt": "Português",
    "ru": "Русский",
    "ar": "العربية",
    "hi": "हिन्दी",
}
RTL = {"ar"}

_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("language", default=None)
_default = [SOURCE]  # the office's staff language, when nothing else is known


@cache
def catalog(code: str) -> dict[str, str]:
    if code == SOURCE or code not in LANGUAGES:
        return {}
    try:
        text = resources.files("ai_employees").joinpath("locales", f"{code}.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    return {k: v for k, v in json.loads(text).items() if v}


def normalize(code: str | None) -> str | None:
    """A supported language for a code like "en-US", "zh-Hans", "pt_BR"; None otherwise."""
    if not code:
        return None
    base = code.replace("_", "-").split("-")[0].lower()
    return base if base in LANGUAGES else None


def best_match(accept_language: str | None) -> str | None:
    """The first supported language of an Accept-Language header."""
    ranked = []
    for i, part in enumerate((accept_language or "").split(",")):
        tag, _, params = part.partition(";")
        tag = tag.strip()
        # "en;q=0.5", "en; q=0.5", "en ; Q = 0.5"
        key, _, q = params.partition("=")
        q = q.strip() if key.strip().lower() == "q" else ""
        try:
            weight = float(q) if q else 1.0
        except ValueError:
            weight = 0.0
        if (code := normalize(tag)) and weight > 0:
            ranked.append((-weight, i, code))
    return min(ranked)[2] if ranked else None


def set_default(code: str | None) -> None:
    _default[0] = normalize(code) or SOURCE


def default() -> str:
    return _default[0]


def current() -> str:
    return _current.get() or _default[0]


@contextmanager
def use_language(code: str | None) -> Iterator[str]:
    """Texts made inside come out in this language (unknown: the office's default)."""
    lang = normalize(code) or _default[0]
    token = _current.set(lang)
    try:
        yield lang
    finally:
        _current.reset(token)


def activate(code: str | None) -> str:
    """Set the language for the rest of this task (a request handler, a chat command)."""
    lang = normalize(code) or _default[0]
    _current.set(lang)
    return lang


def tr(text: str, *args: Any, lang: str | None = None) -> str:
    """A text in the current language, with its values filled in."""
    code = lang or current()
    out = catalog(code).get(text, text) if code != SOURCE else text
    if not args:
        return out
    try:
        return out.format(*args)
    except (IndexError, KeyError, ValueError):  # a broken translation: the source still works
        log.warning("i18n: bad translation into %s of %r", code, text)
        return text.format(*args)


@cache
def ui_texts() -> frozenset[str]:
    """The admin UI's texts (the part of the catalogs the browser loads)."""
    try:
        text = resources.files("ai_employees").joinpath("locales", "source.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        return frozenset()
    return frozenset(json.loads(text).get("ui", []))


_ = tr

# thousands separators: 4.500.000 (vi, de, es, pt, id), 4 500 000 (fr, ru), else 4,500,000
_GROUP = {"vi": ".", "de": ".", "es": ".", "pt": ".", "id": ".", "fr": "\u202f", "ru": "\u202f"}


def number(value: Any) -> str:
    """A number as readers of the current language write it."""
    text = f"{value:,}"
    sep = _GROUP.get(current(), ",")
    if sep == ",":
        return text
    return text.replace(",", "\0").replace(".", "," if sep != "," else ".").replace("\0", sep)


def has_catalog(code: str | None) -> bool:
    return code == SOURCE or bool(code and catalog(code))
