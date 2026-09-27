"""Translate the catalogs in batches (for translators and translation tools).

    python scripts/i18n_batch.py todo <lang> [size]      -> locales/.work/<lang>-todo.json
    python scripts/i18n_batch.py done <lang> <file.json>  (a JSON list, same order as the todo)
    python scripts/i18n_batch.py check [lang ...]

`todo` writes the next untranslated texts as a JSON list; the translations come back as
a JSON list in the same order and `done` checks and stores them. `check` reports
translations that lost a placeholder ({0}), a /command, a markup tag or a line break.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

LOCALES = Path(__file__).resolve().parents[1] / "src" / "ai_employees" / "locales"
WORK = LOCALES / ".work"
_PLACE = re.compile(r"\{[^{}]*\}")
_CMD = re.compile(r"(?<![\w/:.])/'?[a-z][a-z_]*")
_TAG = re.compile(r"</?([a-z][a-z0-9]*)\b")


def problems(source: str, text: str) -> list[str]:
    out = []
    if sorted(_PLACE.findall(source)) != sorted(_PLACE.findall(text)):
        out.append(f"placeholders {sorted(_PLACE.findall(source))} -> {sorted(_PLACE.findall(text))}")
    if sorted(_CMD.findall(source)) != sorted(_CMD.findall(text)):
        out.append(f"commands {sorted(_CMD.findall(source))} -> {sorted(_CMD.findall(text))}")
    if sorted(_TAG.findall(source)) != sorted(_TAG.findall(text)):
        out.append("markup tags differ")
    if source.count("\n") != text.count("\n"):
        out.append("line breaks differ")
    if (source[:1].isspace(), source[-1:].isspace()) != (text[:1].isspace(), text[-1:].isspace()):
        out.append("leading/trailing space differs")
    return out


def load(lang: str) -> dict[str, str]:
    return json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8"))


def save(lang: str, data: dict[str, str]) -> None:
    (LOCALES / f"{lang}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )


def todo(lang: str, size: int = 150) -> None:
    data = load(lang)
    batch = [k for k, v in data.items() if not v][:size]
    WORK.mkdir(exist_ok=True)
    (WORK / f"{lang}-todo.json").write_text(
        json.dumps(batch, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    left = sum(1 for v in data.values() if not v)
    print(f"{lang}: {len(batch)} texts in {WORK / f'{lang}-todo.json'} ({left} untranslated in all)")


def done(lang: str, file: str) -> None:
    data = load(lang)
    batch = json.loads((WORK / f"{lang}-todo.json").read_text(encoding="utf-8"))
    out = json.loads(Path(file).read_text(encoding="utf-8"))
    if not isinstance(out, list) or len(out) != len(batch):
        sys.exit(
            f"expected a JSON list of {len(batch)} translations, got {len(out) if isinstance(out, list) else type(out)}"
        )
    bad = 0
    for source, text in zip(batch, out, strict=True):
        if issues := problems(source, text):
            bad += 1
            print(f"NOT STORED: {source[:60]!r}: {'; '.join(issues)}")
            continue
        data[source] = text
    save(lang, data)
    print(f"{lang}: stored {len(batch) - bad}, rejected {bad}; {sum(1 for v in data.values() if not v)} left")


def check(langs: list[str]) -> int:
    count = 0
    for path in sorted(LOCALES.glob("*.json")):
        lang = path.stem
        if lang == "source" or (langs and lang not in langs):
            continue
        for source, text in load(lang).items():
            if text and (issues := problems(source, text)):
                count += 1
                print(f"{lang}: {source[:60]!r}: {'; '.join(issues)}")
    return count


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    if cmd == "todo":
        todo(args[0], int(args[1]) if len(args) > 1 else 150)
    elif cmd == "done":
        done(args[0], args[1])
    elif cmd == "check":
        sys.exit(1 if check(args) else 0)
