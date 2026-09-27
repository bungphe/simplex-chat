"""Help-desk settings for the unified inbox, edited by admins in the web UI:

- labels:  names and colours staff can put on conversations ("VIP", "Khiếu nại"…);
- canned:  saved replies, inserted into the reply box ({name} becomes the customer's name);
- teams:   groups of staff accounts a conversation can be assigned to;
- rules:   triage on every customer message: when the channel and keywords match, add
           labels, assign a team or person (if nobody has it yet), or hand it to a person
           (the AI stops answering);
- sla_minutes: how fast a waiting customer should get an answer.

Kept as one document in the office database, shared by every process.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .db import DocStore

KEY = "desk"
DEFAULT: dict[str, Any] = {"labels": [], "canned": [], "teams": [], "rules": [], "sla_minutes": 15}
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


def fold(text: str) -> str:
    """Lower case without accents: "Khiếu nại" matches "khieu nai" and "KHIẾU NẠI"."""
    decomposed = unicodedata.normalize("NFD", text.casefold().replace("đ", "d"))
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def _text(value: Any, name: str, limit: int, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{name} là bắt buộc")
    if len(value) > limit:
        raise ValueError(f"{name} dài quá {limit} ký tự")
    return value.strip()


def _names(value: Any, name: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"{name} phải là danh sách")
    return [x.strip() for x in value if x.strip()]


class Desk:
    def __init__(self, docs: DocStore):
        self.docs = docs

    def get(self) -> dict[str, Any]:
        return {**DEFAULT, **(self.docs.get(KEY) or {})}

    @property
    def labels(self) -> list[dict[str, str]]:
        return self.get()["labels"]

    @property
    def teams(self) -> list[dict[str, Any]]:
        return self.get()["teams"]

    def teams_of(self, username: str) -> list[str]:
        return [t["id"] for t in self.teams if username in t.get("members", [])]

    def sla_seconds(self) -> int:
        return int(self.get()["sla_minutes"]) * 60

    # editing (admins); each section is replaced as a whole, after validation

    def save(self, section: str, value: Any, usernames: set[str], channels: set[str]) -> dict[str, Any]:
        clean = self._validate(section, value, usernames, channels)

        def change(doc: dict[str, Any]) -> None:
            doc.update({**DEFAULT, **doc, section: clean})

        self.docs.update(KEY, change, dict(DEFAULT))
        return self.get()

    def _validate(self, section: str, value: Any, usernames: set[str], channels: set[str]) -> Any:
        current = self.get()
        if section == "sla_minutes":
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 7 * 24 * 60:
                raise ValueError("sla_minutes phải là số phút từ 1 đến 10080")
            return value
        if section not in ("labels", "canned", "teams", "rules") or not isinstance(value, list):
            raise ValueError("unknown section")
        if len(value) > 200:
            raise ValueError("tối đa 200 mục")
        out: list[dict[str, Any]] = []
        if section == "labels":
            for x in value:
                name = _text(x.get("name"), "Tên nhãn", 40)
                color = x.get("color") or "#64748b"
                if not isinstance(color, str) or not _COLOR.match(color):
                    raise ValueError("màu phải có dạng #rrggbb")
                if any(o["name"] == name for o in out):
                    raise ValueError(f"nhãn '{name}' bị trùng")
                out.append({"name": name, "color": color})
        elif section == "canned":
            for x in value:
                out.append(
                    {
                        "id": str(x.get("id") or uuid.uuid4().hex[:8])[:16],
                        "title": _text(x.get("title"), "Tiêu đề", 80),
                        "text": _text(x.get("text"), "Nội dung", 4000),
                    }
                )
        elif section == "teams":
            for x in value:
                tid = str(x.get("id") or "")
                if not _ID.match(tid):
                    raise ValueError("mã nhóm chỉ gồm chữ thường, số, '-' hoặc '_'")
                if any(o["id"] == tid for o in out):
                    raise ValueError(f"nhóm '{tid}' bị trùng")
                members = _names(x.get("members"), "Thành viên")
                if unknown := set(members) - usernames:
                    raise ValueError(f"không có tài khoản: {', '.join(sorted(unknown))}")
                out.append({"id": tid, "name": _text(x.get("name"), "Tên nhóm", 60), "members": members})
        else:  # rules
            labels = {lb["name"] for lb in current["labels"]}
            teams = {t["id"] for t in current["teams"]}
            for x in value:
                rule = {
                    "id": str(x.get("id") or uuid.uuid4().hex[:8])[:16],
                    "name": _text(x.get("name"), "Tên quy tắc", 80),
                    "enabled": bool(x.get("enabled", True)),
                    "channels": _names(x.get("channels"), "Kênh"),
                    "keywords": [k for k in _names(x.get("keywords"), "Từ khoá") if len(k) <= 80],
                    "labels": _names(x.get("labels"), "Nhãn"),
                    "team": str(x.get("team") or ""),
                    "assignee": str(x.get("assignee") or ""),
                    "handoff": bool(x.get("handoff", False)),
                }
                if unknown := set(rule["channels"]) - channels:
                    raise ValueError(f"không có kênh: {', '.join(sorted(unknown))}")
                if unknown := set(rule["labels"]) - labels:
                    raise ValueError(f"chưa khai báo nhãn: {', '.join(sorted(unknown))}")
                if rule["team"] and rule["team"] not in teams:
                    raise ValueError(f"không có nhóm: {rule['team']}")
                if rule["assignee"] and rule["assignee"] not in usernames:
                    raise ValueError(f"không có tài khoản: {rule['assignee']}")
                if not (rule["labels"] or rule["team"] or rule["assignee"] or rule["handoff"]):
                    raise ValueError(f"quy tắc '{rule['name']}' chưa có việc gì để làm")
                out.append(rule)
        return out

    # triage

    def matching_rules(self, channel: str, text: str) -> list[dict[str, Any]]:
        folded = fold(text)
        return [
            r
            for r in self.get()["rules"]
            if r.get("enabled", True)
            and (not r["channels"] or channel in r["channels"])
            and (not r["keywords"] or any(fold(k) in folded for k in r["keywords"]))
        ]
