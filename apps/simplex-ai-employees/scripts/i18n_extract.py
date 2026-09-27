"""Collect the texts to translate into src/ai_employees/locales/.

    python scripts/i18n_extract.py

- the admin UI: tr("...") in static/*.js and the texts written in static/admin.html;
- the server: tr("...") in the Python modules, and the labels of a few module-level
  tables that are translated where they are used (menus, statuses...).

locales/source.json lists them (the "ui" part is what the browser loads); every
locales/<code>.json keeps its translations, gets new texts with an empty translation
(shown in Vietnamese until translated) and loses texts no longer in the code.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "ai_employees"
sys.path.insert(0, str(ROOT.parent))
from ai_employees.i18n import LANGUAGES, SOURCE

VI = re.compile(r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]", re.IGNORECASE)
JS_TR = re.compile(r'\btr\(\s*"((?:[^"\\]|\\.)*)"')
# module-level tables whose labels go through tr() where they are used
TABLES = {
    "STATUS",
    "ADMIN_MENU",
    "ADMIN_HELP",
    "COMMANDS",
    "GROUPS",
    "PAY",
    "SLOTS",
    "ATTACHMENT_KINDS",
    "CONGRATS",
    "SENT",
}


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.texts: set[str] = set()
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        for k, v in attrs:
            if k in ("placeholder", "title", "aria-label") and v and v.strip():
                self.texts.add(v.strip())

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.texts.add(data.strip())


def ui_texts() -> set[str]:
    out: set[str] = set()
    for f in sorted((ROOT / "static").glob("*.js")):
        for m in JS_TR.finditer(f.read_text(encoding="utf-8")):
            out.add(json.loads(f'"{m.group(1)}"'))
    page = _Page()
    page.feed((ROOT / "static" / "admin.html").read_text(encoding="utf-8"))
    out |= {t for t in page.texts if VI.search(t)}
    return out


def _strings(node: ast.AST) -> list[str]:
    """Vietnamese string values in a table (dict values, list items), not dict keys."""
    out: list[str] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if VI.search(node.value):
            out.append(node.value)
    elif isinstance(node, ast.Dict):
        for v in node.values:
            out += _strings(v)
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for v in node.elts:
            out += _strings(v)
    elif isinstance(node, ast.JoinedStr):
        pass
    elif isinstance(node, ast.BinOp):
        out += _strings(node.left) + _strings(node.right)
    return out


def server_texts() -> set[str]:
    out: set[str] = set()
    for f in sorted(ROOT.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("tr", "_")
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                out.add(node.args[0].value)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "tr"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                out.add(node.args[0].value)
        for node in tree.body:
            targets = []
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target.id]
            if set(targets) & TABLES and node.value is not None:
                out |= set(_strings(node.value))
    return out


def main() -> None:
    ui, server = ui_texts(), server_texts()
    locales = ROOT / "locales"
    locales.mkdir(exist_ok=True)
    (locales / "source.json").write_text(
        json.dumps({"ui": sorted(ui), "server": sorted(server - ui)}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    every = ui | server
    for code in LANGUAGES:
        if code == SOURCE:
            continue
        path = locales / f"{code}.json"
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        new = {k: old.get(k, "") for k in sorted(every)}
        path.write_text(json.dumps(new, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        done = sum(1 for v in new.values() if v)
        print(f"{code}: {done}/{len(new)} translated")
    print(f"{len(ui)} admin UI texts, {len(server - ui)} more on the server")


if __name__ == "__main__":
    main()
