"""Projects and tasks of the shop's staff: a mind map, a list and a Kanban board of the
same tree of tasks (the admin UI's "Công việc" page), with the staff's own commands in
their SimpleX chat and a morning reminder of what is late or due.

A project is the root of the map; its tasks form a tree (a task's children are its
branch). Every task has a short code (e.g. H0RFR, used for dependencies and in chat
commands), who does it, a priority, a status, a percentage done, labels, start and due
dates, rich-text notes, a checklist, links, comments and files, an icon, a colour and an
optional round picture. A branch's progress is the average of its children's; a leaf
that "follows its checklist" is as done as its checklist. Late (past the due date) and
due soon (within DUE_SOON_DAYS) are worked out, not stored. Every change is written to
the project's log.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import secrets
from collections.abc import Callable
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, ClassVar

from .db import Database, IntegrityError
from .i18n import tr, use_language
from .state import now_iso

if TYPE_CHECKING:
    from .employee import Office
    from .users import User

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS pm_projects (
  id {autoid}, name TEXT NOT NULL, color TEXT NOT NULL DEFAULT '', owner TEXT NOT NULL DEFAULT '',
  archived {int} NOT NULL DEFAULT 0, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pm_tasks (
  id {autoid}, project_id {int} NOT NULL, parent_id {int}, pos {int} NOT NULL DEFAULT 0,
  code TEXT NOT NULL UNIQUE, title TEXT NOT NULL, icon TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '',
  assignee TEXT NOT NULL DEFAULT '', priority TEXT NOT NULL DEFAULT 'medium', status TEXT NOT NULL DEFAULT 'todo',
  progress {int} NOT NULL DEFAULT 0, labels TEXT NOT NULL DEFAULT '', avatar TEXT NOT NULL DEFAULT '',
  depends TEXT NOT NULL DEFAULT '', start_date TEXT NOT NULL DEFAULT '', due_date TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '', track_checklist {int} NOT NULL DEFAULT 0, checklist TEXT NOT NULL DEFAULT '[]',
  links TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL DEFAULT '', created TEXT NOT NULL,
  updated TEXT NOT NULL, done_at TEXT);
CREATE INDEX IF NOT EXISTS pm_tasks_project ON pm_tasks (project_id, parent_id, pos);
CREATE INDEX IF NOT EXISTS pm_tasks_assignee ON pm_tasks (assignee, status);
CREATE TABLE IF NOT EXISTS pm_comments (
  id {id}, task_id {int} NOT NULL, author TEXT NOT NULL, text TEXT NOT NULL, created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS pm_comments_task ON pm_comments (task_id, id);
CREATE TABLE IF NOT EXISTS pm_files (
  id {id}, task_id {int} NOT NULL, name TEXT NOT NULL, mime TEXT NOT NULL, size {int} NOT NULL,
  data {blob} NOT NULL, author TEXT NOT NULL, created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS pm_files_task ON pm_files (task_id, id);
CREATE TABLE IF NOT EXISTS pm_log (
  id {id}, project_id {int} NOT NULL, task_id {int}, actor TEXT NOT NULL, action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '', ts TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS pm_log_project ON pm_log (project_id, id)
"""
STATUSES = ("todo", "doing", "done", "paused")
PRIORITIES = ("low", "medium", "high", "urgent")
STATUS_TEXT = {"todo": "Chưa bắt đầu", "doing": "Đang làm", "done": "Hoàn thành", "paused": "Tạm dừng"}
PRIORITY_TEXT = {"low": "Thấp", "medium": "Vừa", "high": "Cao", "urgent": "Gấp"}
DUE_SOON_DAYS = 3
MAX_FILE = 10 * 1024 * 1024
MAX_AVATAR = 200_000  # a small round picture, resized in the browser
MAX_NOTES = 50_000
MAX_TASKS = 5_000  # per project
DIGEST_HOUR = 8  # the morning reminder (the office's local time)
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_AVATAR = re.compile(r"^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$")
TASK_FIELDS = (
    "title",
    "icon",
    "color",
    "assignee",
    "priority",
    "status",
    "progress",
    "labels",
    "avatar",
    "depends",
    "start_date",
    "due_date",
    "notes",
    "track_checklist",
    "checklist",
    "links",
)
CSV_COLUMNS = (
    "code",
    "parent",
    "title",
    "assignee",
    "priority",
    "status",
    "progress",
    "labels",
    "start_date",
    "due_date",
    "depends",
)


class ProjectError(ValueError):
    """A request that cannot be done (shown to the user as is)."""


# ---------------------------------------------------------------------- #
# rich-text notes: only simple formatting survives


class _Cleaner(HTMLParser):
    ALLOWED: ClassVar[set[str]] = {
        "p",
        "br",
        "b",
        "strong",
        "i",
        "em",
        "u",
        "s",
        "strike",
        "ul",
        "ol",
        "li",
        "blockquote",
        "h1",
        "h2",
        "h3",
        "a",
        "code",
        "pre",
        "div",
        "span",
    }
    VOID: ClassVar[set[str]] = {"br"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.open: list[str] = []
        self.skip = 0  # inside <script>/<style>: dropped with their content

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "iframe", "object", "template"):
            self.skip += 1
            return
        if self.skip or tag not in self.ALLOWED:
            return
        extra = ""
        if tag == "a":
            href = next((v or "" for k, v in attrs if k == "href"), "").strip()
            if not re.match(r"^(https?://|mailto:)", href, re.IGNORECASE):
                return
            extra = f' href="{_attr(href)}" rel="noopener noreferrer" target="_blank"'
        self.out.append(f"<{tag}{extra}>")
        if tag not in self.VOID:
            self.open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "iframe", "object", "template"):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in self.open:
            return
        while self.open:  # close whatever was left open inside it too
            last = self.open.pop()
            self.out.append(f"</{last}>")
            if last == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def clean_html(text: str) -> str:
    """The notes' HTML with only simple formatting and http(s)/mailto links kept."""
    cleaner = _Cleaner()
    cleaner.feed(text or "")
    cleaner.close()
    cleaner.out += [f"</{t}>" for t in reversed(cleaner.open)]
    return "".join(cleaner.out)


def plain(html_text: str) -> str:
    """The notes as plain text (for chat and CSV)."""
    text = re.sub(r"<(br|/p|/li|/div|/h\d)>", "\n", html_text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(
        r"\n{3,}", "\n\n", text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    ).strip()


def _today() -> date:
    return datetime.now().astimezone().date()


class Projects:
    def __init__(self, office: Office):
        self.office = office
        self.db: Database = office.hub.inbox.db  # with the shop's other records
        self.db.script(SCHEMA)
        self._digest_day = ""

    # ------------------------------------------------------------------ #
    # people

    def people(self) -> list[dict[str, str]]:
        """Who tasks can be given to: the staff accounts (and the owner)."""
        from .users import Users

        users = Users(self.office.docs, "")
        return [
            {"username": u["username"], "name": u["name"] or u["username"]}
            for u in users.list()
            if not u.get("disabled")
        ]

    # ------------------------------------------------------------------ #
    # projects

    def projects(self, archived: bool = False) -> list[dict[str, Any]]:
        rows = self.db.rows(
            "SELECT * FROM pm_projects WHERE archived=? ORDER BY updated DESC, id DESC",
            (1 if archived else 0,),
        )
        return [self._project_summary(p) for p in rows]

    def project(self, pid: int) -> dict[str, Any]:
        row = self.db.row("SELECT * FROM pm_projects WHERE id=?", (pid,))
        if row is None:
            raise KeyError(tr("dự án {0}", pid))
        return row

    def _project_summary(self, p: dict[str, Any]) -> dict[str, Any]:
        tasks = self._computed(self._rows(int(p["id"])))
        return {**p, **self._stats(tasks)}

    def _stats(self, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        leaves = [t for t in tasks if not t["children"]]
        done = sum(t["status"] == "done" for t in leaves)
        tops = [t for t in tasks if t["parent_id"] is None]
        progress = round(sum(t["done_pct"] for t in tops) / len(tops)) if tops else 0
        return {
            "task_count": len(leaves),
            "done_count": done,
            "progress": progress,
            "late_count": sum(t["late"] for t in tasks),
            "soon_count": sum(t["due_soon"] for t in tasks),
        }

    def save_project(self, pid: int | None, data: dict[str, Any], actor: str) -> dict[str, Any]:
        name = str(data.get("name", "")).strip()[:120]
        color = str(data.get("color", "") or "")
        if color and not _COLOR.match(color):
            raise ProjectError(tr("Màu không hợp lệ"))
        now = now_iso()
        if pid is None:
            if not name:
                raise ProjectError(tr("Cần tên dự án"))
            pid = self.db.execute(
                "INSERT INTO pm_projects (name, color, owner, created, updated) VALUES (?, ?, ?, ?, ?) RETURNING id",
                (name, color, actor, now, now),
            )
            assert pid is not None
            self._log(int(pid), None, actor, "project_created", name)
            return self._project_summary(self.project(int(pid)))
        self.project(pid)  # exists
        fields: dict[str, Any] = {}
        if "name" in data:
            if not name:
                raise ProjectError(tr("Cần tên dự án"))
            fields["name"] = name
        if "color" in data:
            fields["color"] = color
        if "archived" in data:
            fields["archived"] = 1 if data["archived"] else 0
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(
                f"UPDATE pm_projects SET {sets}, updated=? WHERE id=?", [*fields.values(), now, pid]
            )
            self._log(pid, None, actor, "project_updated", ", ".join(f"{k}: {v}" for k, v in fields.items()))
        return self._project_summary(self.project(pid))

    def delete_project(self, pid: int, actor: str) -> None:
        self.project(pid)
        with self.db.transaction():
            ids = [int(r["id"]) for r in self.db.rows("SELECT id FROM pm_tasks WHERE project_id=?", (pid,))]
            self._delete_task_data(ids)
            self.db.execute("DELETE FROM pm_tasks WHERE project_id=?", (pid,))
            self.db.execute("DELETE FROM pm_log WHERE project_id=?", (pid,))
            self.db.execute("DELETE FROM pm_projects WHERE id=?", (pid,))
        log.info("projects: %s deleted project %s", actor, pid)

    def _delete_task_data(self, ids: list[int]) -> None:
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ", ".join("?" * len(chunk))
            self.db.execute(f"DELETE FROM pm_comments WHERE task_id IN ({marks})", chunk)
            self.db.execute(f"DELETE FROM pm_files WHERE task_id IN ({marks})", chunk)

    # ------------------------------------------------------------------ #
    # tasks

    def _rows(self, pid: int) -> list[dict[str, Any]]:
        return self.db.rows("SELECT * FROM pm_tasks WHERE project_id=? ORDER BY pos, id", (pid,))

    def _row(self, tid: int) -> dict[str, Any]:
        row = self.db.row("SELECT * FROM pm_tasks WHERE id=?", (tid,))
        if row is None:
            raise KeyError(tr("công việc {0}", tid))
        return row

    def by_code(self, code: str) -> dict[str, Any] | None:
        row = self.db.row("SELECT * FROM pm_tasks WHERE code=?", (code.strip().upper(),))
        return self.task(int(row["id"])) if row else None

    def board(self, pid: int) -> dict[str, Any]:
        """A project with all its tasks (flat, in order, with what is worked out)."""
        project = self.project(pid)
        tasks = self._computed(self._rows(pid))
        counts = {
            int(r["task_id"]): int(r["n"])
            for r in self.db.rows(
                "SELECT c.task_id, COUNT(*) AS n FROM pm_comments c JOIN pm_tasks t ON t.id=c.task_id "
                "WHERE t.project_id=? GROUP BY c.task_id",
                (pid,),
            )
        }
        files = {
            int(r["task_id"]): int(r["n"])
            for r in self.db.rows(
                "SELECT f.task_id, COUNT(*) AS n FROM pm_files f JOIN pm_tasks t ON t.id=f.task_id "
                "WHERE t.project_id=? GROUP BY f.task_id",
                (pid,),
            )
        }
        for t in tasks:
            t["comment_count"] = counts.get(int(t["id"]), 0)
            t["file_count"] = files.get(int(t["id"]), 0)
        return {"project": {**project, **self._stats(tasks)}, "tasks": tasks}

    def task(self, tid: int) -> dict[str, Any]:
        row = self._row(tid)
        tasks = self._computed(self._rows(int(row["project_id"])))
        task = next(t for t in tasks if t["id"] == row["id"])
        task["comment_count"] = int(
            self.db.row("SELECT COUNT(*) AS n FROM pm_comments WHERE task_id=?", (tid,))["n"]
        )
        task["file_count"] = int(
            self.db.row("SELECT COUNT(*) AS n FROM pm_files WHERE task_id=?", (tid,))["n"]
        )
        return task

    def _computed(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Children, progress of branches, done/total leaves, late and due soon, what
        each task still waits for."""
        today = _today()
        soon = (today + timedelta(days=DUE_SOON_DAYS)).isoformat()
        tasks = [self._decode(r) for r in rows]
        by_id = {int(t["id"]): t for t in tasks}
        by_code = {t["code"]: t for t in tasks}
        for t in tasks:
            t["children"] = []
        for t in tasks:
            parent = by_id.get(int(t["parent_id"])) if t["parent_id"] is not None else None
            if parent is not None:
                parent["children"].append(int(t["id"]))
            elif t["parent_id"] is not None:  # a lost parent: shown at the top
                t["parent_id"] = None

        def walk(t: dict[str, Any], depth: int) -> tuple[float, int, int]:
            t["depth"] = depth
            if t["children"]:
                results = [walk(by_id[c], depth + 1) for c in t["children"]]
                pct = sum(r[0] for r in results) / len(results)
                leaves = sum(r[1] for r in results)
                done = sum(r[2] for r in results)
            else:
                items = t["checklist"]
                if t["status"] == "done":
                    pct = 100.0
                elif t["track_checklist"] and items:
                    pct = 100.0 * sum(bool(i.get("done")) for i in items) / len(items)
                else:
                    pct = float(t["progress"])
                leaves, done = 1, int(t["status"] == "done")
            t["done_pct"] = round(pct)
            t["leaf_total"], t["leaf_done"] = leaves, done
            return pct, leaves, done

        for t in tasks:
            if t["parent_id"] is None:
                walk(t, 0)
        for t in tasks:
            t.setdefault("depth", 0)
            t.setdefault("done_pct", t["progress"])
            open_ = t["status"] != "done"
            t["late"] = bool(open_ and t["due_date"] and t["due_date"] < today.isoformat())
            t["due_soon"] = bool(open_ and not t["late"] and t["due_date"] and t["due_date"] <= soon)
            t["waiting_for"] = [
                c for c in t["depends_list"] if c in by_code and by_code[c]["status"] != "done"
            ]
        return tasks

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        t = dict(row)
        try:
            items = json.loads(t["checklist"] or "[]")
        except ValueError:
            items = []
        t["checklist"] = [i for i in items if isinstance(i, dict)]
        t["track_checklist"] = bool(t["track_checklist"])
        t["labels_list"] = [x.strip() for x in t["labels"].split(",") if x.strip()]
        t["depends_list"] = [x.strip().upper() for x in re.split(r"[,\s]+", t["depends"]) if x.strip()]
        t["links_list"] = [x.strip() for x in t["links"].splitlines() if x.strip()]
        return t

    def _new_code(self) -> str:
        for _ in range(20):
            code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(5))
            if self.db.row("SELECT 1 AS x FROM pm_tasks WHERE code=?", (code,)) is None:
                return code
        raise ProjectError(tr("Không tạo được mã công việc, thử lại"))

    def _clean(self, pid: int, data: dict[str, Any], tid: int | None = None) -> dict[str, Any]:
        """The fields of a task from a form, checked; only those present."""
        out: dict[str, Any] = {}
        for key in TASK_FIELDS:
            if key not in data:
                continue
            value = data[key]
            if key == "title":
                value = " ".join(str(value or "").split())[:300]
                if not value:
                    raise ProjectError(tr("Cần tên công việc"))
            elif key == "icon":
                value = str(value or "")[:8]
            elif key == "color":
                value = str(value or "")
                if value and not _COLOR.match(value):
                    raise ProjectError(tr("Màu không hợp lệ"))
            elif key == "assignee":
                value = str(value or "").strip()[:40]
            elif key == "priority":
                if value not in PRIORITIES:
                    raise ProjectError(tr("Mức ưu tiên không hợp lệ"))
            elif key == "status":
                if value not in STATUSES:
                    raise ProjectError(tr("Trạng thái không hợp lệ"))
            elif key == "progress":
                try:
                    value = max(0, min(100, int(value or 0)))
                except (TypeError, ValueError):
                    raise ProjectError(tr("% hoàn thành phải là số từ 0 đến 100")) from None
            elif key == "labels":
                if isinstance(value, list):
                    value = ",".join(str(x) for x in value)
                value = ", ".join(x.strip()[:40] for x in str(value or "").split(",") if x.strip())[:400]
            elif key == "avatar":
                value = str(value or "")
                if value and (len(value) > MAX_AVATAR or not _AVATAR.match(value)):
                    raise ProjectError(tr("Ảnh không hợp lệ hoặc quá lớn"))
            elif key == "depends":
                if isinstance(value, list):
                    value = ",".join(str(x) for x in value)
                codes = [x.strip().upper() for x in re.split(r"[,\s]+", str(value or "")) if x.strip()]
                own = self.db.row("SELECT code FROM pm_tasks WHERE id=?", (tid,)) if tid else None
                for code in codes:
                    row = self.db.row("SELECT project_id FROM pm_tasks WHERE code=?", (code,))
                    if row is None or int(row["project_id"]) != pid:
                        raise ProjectError(tr("Không có công việc mã {0} trong dự án", code))
                    if own and own["code"] == code:
                        raise ProjectError(tr("Một việc không phụ thuộc vào chính nó"))
                if own and codes:
                    self._no_loop(pid, own["code"], codes)
                value = ", ".join(dict.fromkeys(codes))
            elif key in ("start_date", "due_date"):
                value = str(value or "")[:10]
                if value and not _DATE.match(value):
                    raise ProjectError(tr("Ngày phải có dạng YYYY-MM-DD"))
            elif key == "notes":
                value = str(value or "")
                if len(value) > MAX_NOTES:
                    raise ProjectError(tr("Ghi chú quá dài"))
                value = clean_html(value)
            elif key == "track_checklist":
                value = 1 if value else 0
            elif key == "checklist":
                if not isinstance(value, list):
                    raise ProjectError(tr("Danh sách việc cần làm không hợp lệ"))
                items = []
                for item in value[:100]:
                    if isinstance(item, dict) and str(item.get("text", "")).strip():
                        items.append(
                            {"text": str(item["text"]).strip()[:300], "done": bool(item.get("done"))}
                        )
                value = json.dumps(items, ensure_ascii=False)
            elif key == "links":
                lines = value if isinstance(value, list) else str(value or "").splitlines()
                kept = []
                for line in lines[:30]:
                    line = str(line).strip()
                    if not line:
                        continue
                    if not re.match(r"^https?://\S+$", line):
                        raise ProjectError(tr("Liên kết phải bắt đầu bằng http:// hoặc https://"))
                    kept.append(line[:500])
                value = "\n".join(kept)
            out[key] = value
        start, due = out.get("start_date"), out.get("due_date")
        if tid is not None and (start is None or due is None):
            row = self._row(tid)
            start = row["start_date"] if start is None else start
            due = row["due_date"] if due is None else due
        if start and due and due < start:
            raise ProjectError(tr("Ngày kết thúc trước ngày bắt đầu"))
        return out

    def _no_loop(self, pid: int, code: str, depends: list[str]) -> None:
        """Refuse dependencies that lead back to the task itself (A waits for B, B for A)."""
        graph = {
            r["code"]: [x.strip().upper() for x in re.split(r"[,\s]+", r["depends"]) if x.strip()]
            for r in self.db.rows("SELECT code, depends FROM pm_tasks WHERE project_id=?", (pid,))
        }
        seen: set[str] = set()
        todo = list(depends)
        while todo:
            c = todo.pop()
            if c == code:
                raise ProjectError(tr("Phụ thuộc vòng tròn: {0} lại phải chờ chính nó", code))
            if c not in seen:
                seen.add(c)
                todo += graph.get(c, [])

    def create_task(
        self,
        pid: int,
        data: dict[str, Any],
        actor: str,
        parent_id: int | None = None,
        after_id: int | None = None,
    ) -> dict[str, Any]:
        self.project(pid)
        if (
            int(self.db.row("SELECT COUNT(*) AS n FROM pm_tasks WHERE project_id=?", (pid,))["n"])
            >= MAX_TASKS
        ):
            raise ProjectError(tr("Dự án đã có quá nhiều công việc"))
        if parent_id is not None and int(self._row(parent_id)["project_id"]) != pid:
            raise ProjectError(tr("Nhánh cha không thuộc dự án này"))
        fields = self._clean(pid, {"title": tr("Công việc mới"), **data})
        now = now_iso()
        with self.db.transaction():
            siblings = self._siblings(pid, parent_id)
            if after_id is not None and after_id in siblings:
                pos = siblings.index(after_id) + 1
            else:
                pos = len(siblings)
            for i, sid in enumerate(siblings[pos:], start=pos + 1):
                self.db.execute("UPDATE pm_tasks SET pos=? WHERE id=?", (i, sid))
            cols = ["project_id", "parent_id", "pos", "code", "created_by", "created", "updated", *fields]
            values = [pid, parent_id, pos, self._new_code(), actor, now, now, *fields.values()]
            if fields.get("status") == "done":
                cols.append("done_at")
                values.append(now)
            tid = self.db.execute(
                f"INSERT INTO pm_tasks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) RETURNING id",
                values,
            )
            assert tid is not None
            self._touch(pid)
        task = self.task(int(tid))
        self._log(pid, int(tid), actor, "created", f"{task['code']} {task['title']}")
        if task["assignee"] and task["assignee"] != actor:
            self._spawn_assigned(task, actor)
        return task

    def _siblings(self, pid: int, parent_id: int | None) -> list[int]:
        if parent_id is None:
            rows = self.db.rows(
                "SELECT id FROM pm_tasks WHERE project_id=? AND parent_id IS NULL ORDER BY pos, id", (pid,)
            )
        else:
            rows = self.db.rows("SELECT id FROM pm_tasks WHERE parent_id=? ORDER BY pos, id", (parent_id,))
        return [int(r["id"]) for r in rows]

    def update_task(self, tid: int, data: dict[str, Any], actor: str) -> dict[str, Any]:
        row = self._row(tid)
        pid = int(row["project_id"])
        fields = self._clean(pid, data, tid)
        if "parent_id" in data:
            self.move(tid, actor, parent_id=data["parent_id"])
        changed = {k: v for k, v in fields.items() if row.get(k) != v}
        if changed:
            now = now_iso()
            if "status" in changed:
                changed["done_at"] = now if changed["status"] == "done" else None
            sets = ", ".join(f"{k}=?" for k in changed)
            self.db.execute(
                f"UPDATE pm_tasks SET {sets}, updated=? WHERE id=?", [*changed.values(), now, tid]
            )
            self._touch(pid)
            shown = [k for k in changed if k not in ("done_at", "avatar", "notes", "checklist")]
            detail = "; ".join(f"{k}: {row.get(k)} → {changed[k]}" for k in shown)
            if "notes" in changed:
                detail = "; ".join(filter(None, [detail, "notes"]))
            if "checklist" in changed:
                detail = "; ".join(filter(None, [detail, "checklist"]))
            if "avatar" in changed:
                detail = "; ".join(filter(None, [detail, "avatar"]))
            self._log(pid, tid, actor, "updated", f"{row['code']}: {detail}"[:1000])
        task = self.task(tid)
        if "assignee" in changed and task["assignee"] and task["assignee"] != actor:
            self._spawn_assigned(task, actor)
        return task

    def move(self, tid: int, actor: str, parent_id: Any = "keep", direction: str = "") -> dict[str, Any]:
        """To another branch (parent_id; None: the top), or up/down among its siblings."""
        row = self._row(tid)
        pid = int(row["project_id"])
        with self.db.transaction():
            if parent_id != "keep":
                new_parent = None if parent_id in (None, "", 0) else int(parent_id)
                if new_parent is not None:
                    target = self._row(new_parent)
                    if int(target["project_id"]) != pid:
                        raise ProjectError(tr("Nhánh cha không thuộc dự án này"))
                    if new_parent == tid or new_parent in self._descendants(tid):
                        raise ProjectError(tr("Không chuyển được một nhánh vào trong chính nó"))
                if new_parent != row["parent_id"]:
                    old = self._siblings(pid, row["parent_id"])
                    old.remove(tid)
                    for i, sid in enumerate(old):
                        self.db.execute("UPDATE pm_tasks SET pos=? WHERE id=?", (i, sid))
                    pos = len(self._siblings(pid, new_parent))
                    self.db.execute(
                        "UPDATE pm_tasks SET parent_id=?, pos=?, updated=? WHERE id=?",
                        (new_parent, pos, now_iso(), tid),
                    )
                    self._log(pid, tid, actor, "moved", row["code"])
            if direction in ("up", "down"):
                siblings = self._siblings(pid, self._row(tid)["parent_id"])
                i = siblings.index(tid)
                j = i - 1 if direction == "up" else i + 1
                if 0 <= j < len(siblings):
                    siblings[i], siblings[j] = siblings[j], siblings[i]
                    for k, sid in enumerate(siblings):
                        self.db.execute("UPDATE pm_tasks SET pos=? WHERE id=?", (k, sid))
            self._touch(pid)
        return self.task(tid)

    def _descendants(self, tid: int) -> set[int]:
        out: set[int] = set()
        todo = [tid]
        while todo:
            rows = self.db.rows(
                f"SELECT id FROM pm_tasks WHERE parent_id IN ({', '.join('?' * len(todo))})", todo
            )
            todo = [int(r["id"]) for r in rows if int(r["id"]) not in out]
            out.update(todo)
        return out

    def delete_task(self, tid: int, actor: str) -> int:
        """The task and its whole branch; returns how many tasks were removed."""
        row = self._row(tid)
        pid = int(row["project_id"])
        ids = [tid, *self._descendants(tid)]
        with self.db.transaction():
            self._delete_task_data(ids)
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                self.db.execute(f"DELETE FROM pm_tasks WHERE id IN ({', '.join('?' * len(chunk))})", chunk)
            for k, sid in enumerate(self._siblings(pid, row["parent_id"])):
                self.db.execute("UPDATE pm_tasks SET pos=? WHERE id=?", (k, sid))
            self._touch(pid)
        self._log(pid, None, actor, "deleted", f"{row['code']} {row['title']} (+{len(ids) - 1})")
        return len(ids)

    def _touch(self, pid: int) -> None:
        self.db.execute("UPDATE pm_projects SET updated=? WHERE id=?", (now_iso(), pid))

    # ------------------------------------------------------------------ #
    # comments, files, log

    def comments(self, tid: int) -> list[dict[str, Any]]:
        self._row(tid)
        return self.db.rows("SELECT * FROM pm_comments WHERE task_id=? ORDER BY id", (tid,))

    def add_comment(self, tid: int, text: str, actor: str) -> dict[str, Any]:
        row = self._row(tid)
        text = str(text or "").strip()[:4000]
        if not text:
            raise ProjectError(tr("Bình luận trống"))
        cid = self.db.execute(
            "INSERT INTO pm_comments (task_id, author, text, created) VALUES (?, ?, ?, ?) RETURNING id",
            (tid, actor, text, now_iso()),
        )
        self._log(int(row["project_id"]), tid, actor, "commented", f"{row['code']}: {text[:200]}")
        if row["assignee"] and row["assignee"] != actor:
            self._spawn_notify(
                row["assignee"],
                lambda: tr("💬 {0} bình luận việc {1} «{2}»:\n{3}", actor, row["code"], row["title"], text),
            )
        return self.db.row("SELECT * FROM pm_comments WHERE id=?", (cid,)) or {}

    def delete_comment(self, comment_id: int, actor: str, manager: bool) -> None:
        row = self.db.row("SELECT * FROM pm_comments WHERE id=?", (comment_id,))
        if row is None:
            raise KeyError(tr("bình luận {0}", comment_id))
        if row["author"] != actor and not manager:
            raise PermissionError(tr("Chỉ người viết hoặc quản lý xoá được bình luận"))
        self.db.execute("DELETE FROM pm_comments WHERE id=?", (comment_id,))

    def files(self, tid: int) -> list[dict[str, Any]]:
        self._row(tid)
        return self.db.rows(
            "SELECT id, task_id, name, mime, size, author, created FROM pm_files WHERE task_id=? ORDER BY id",
            (tid,),
        )

    def add_file(self, tid: int, name: str, mime: str, data: bytes, actor: str) -> dict[str, Any]:
        row = self._row(tid)
        name = re.sub(r"[\x00-\x1f/\\]", "_", (name or "file").strip())[:200] or "file"
        mime = mime if re.match(r"^[\w.+-]+/[\w.+-]+$", mime or "") else "application/octet-stream"
        if not data:
            raise ProjectError(tr("Tệp trống"))
        if len(data) > MAX_FILE:
            raise ProjectError(tr("Tệp quá lớn (tối đa {0} MB)", MAX_FILE // 1024 // 1024))
        fid = self.db.execute(
            "INSERT INTO pm_files (task_id, name, mime, size, data, author, created) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (tid, name, mime, len(data), data, actor, now_iso()),
        )
        self._log(int(row["project_id"]), tid, actor, "file_added", f"{row['code']}: {name}")
        return {"id": fid, "task_id": tid, "name": name, "mime": mime, "size": len(data), "author": actor}

    def file(self, fid: int) -> dict[str, Any]:
        row = self.db.row("SELECT * FROM pm_files WHERE id=?", (fid,))
        if row is None:
            raise KeyError(tr("tệp {0}", fid))
        return {**row, "data": bytes(row["data"])}

    def delete_file(self, fid: int, actor: str, manager: bool) -> None:
        row = self.db.row(
            "SELECT f.id, f.author, f.name, t.project_id, t.id AS task_id FROM pm_files f "
            "JOIN pm_tasks t ON t.id=f.task_id WHERE f.id=?",
            (fid,),
        )
        if row is None:
            raise KeyError(tr("tệp {0}", fid))
        if row["author"] != actor and not manager:
            raise PermissionError(tr("Chỉ người tải lên hoặc quản lý xoá được tệp"))
        self.db.execute("DELETE FROM pm_files WHERE id=?", (fid,))
        self._log(int(row["project_id"]), int(row["task_id"]), actor, "file_deleted", row["name"])

    def _log(self, pid: int, tid: int | None, actor: str, action: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO pm_log (project_id, task_id, actor, action, detail, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (pid, tid, actor, action, detail[:1000], now_iso()),
        )

    def log(self, pid: int, limit: int = 200) -> list[dict[str, Any]]:
        self.project(pid)
        return self.db.rows(
            "SELECT * FROM pm_log WHERE project_id=? ORDER BY id DESC LIMIT ?",
            (pid, max(1, min(limit, 1000))),
        )

    # ------------------------------------------------------------------ #
    # files of a whole project: JSON (to move or keep a copy) and CSV

    def export(self, pid: int) -> dict[str, Any]:
        project = self.project(pid)
        rows = self._rows(pid)
        codes = {int(r["id"]): r["code"] for r in rows}
        tasks = []
        for r in rows:
            t = {k: r[k] for k in ("code", *TASK_FIELDS, "created", "created_by", "done_at")}
            t["checklist"] = json.loads(r["checklist"] or "[]")
            t["track_checklist"] = bool(r["track_checklist"])
            t["parent"] = codes.get(int(r["parent_id"])) if r["parent_id"] is not None else None
            t["comments"] = [
                {"author": c["author"], "text": c["text"], "created": c["created"]}
                for c in self.db.rows("SELECT * FROM pm_comments WHERE task_id=? ORDER BY id", (r["id"],))
            ]
            tasks.append(t)
        return {
            "format": "ai-employees-project",
            "version": 1,
            "exported": now_iso(),
            "project": {"name": project["name"], "color": project["color"]},
            "tasks": tasks,
        }

    def import_project(self, data: dict[str, Any], actor: str) -> dict[str, Any]:
        """A project from an export (a new one, with new codes; files are not in exports)."""
        if not isinstance(data, dict) or data.get("format") != "ai-employees-project":
            raise ProjectError(tr("Không phải tệp dự án"))
        tasks = data.get("tasks")
        if not isinstance(tasks, list) or len(tasks) > MAX_TASKS:
            raise ProjectError(tr("Tệp dự án không hợp lệ"))
        meta = data.get("project") or {}
        name = str(meta.get("name") or tr("Dự án nhập"))
        project = self.save_project(None, {"name": name, "color": meta.get("color", "")}, actor)
        pid = int(project["id"])
        new_ids: dict[str, int] = {}
        pending = [t for t in tasks if isinstance(t, dict)]
        try:
            for _ in range(len(pending) + 1):  # parents before children, whatever the order
                later = []
                for t in pending:
                    parent = t.get("parent")
                    if parent and parent not in new_ids and any(x.get("code") == parent for x in pending):
                        later.append(t)
                        continue
                    fields = {k: t[k] for k in TASK_FIELDS if k in t and k != "depends"}
                    created = self.create_task(
                        pid, fields, actor, parent_id=new_ids.get(parent) if parent else None
                    )
                    new_ids[str(t.get("code") or created["code"])] = int(created["id"])
                    for c in t.get("comments") or []:
                        if isinstance(c, dict) and str(c.get("text", "")).strip():
                            self.db.execute(
                                "INSERT INTO pm_comments (task_id, author, text, created) VALUES (?, ?, ?, ?)",
                                (
                                    created["id"],
                                    str(c.get("author") or actor)[:40],
                                    str(c["text"])[:4000],
                                    str(c.get("created") or now_iso())[:40],
                                ),
                            )
                if not later or len(later) == len(pending):
                    break
                pending = later
            codes = {old: self._row(i)["code"] for old, i in new_ids.items()}
            for t in tasks:  # dependencies, with the new codes
                if isinstance(t, dict) and t.get("depends") and t.get("code") in new_ids:
                    deps = [
                        codes[c.strip()] for c in re.split(r"[,\s]+", str(t["depends"])) if c.strip() in codes
                    ]
                    if deps:
                        self.db.execute(
                            "UPDATE pm_tasks SET depends=? WHERE id=?", (", ".join(deps), new_ids[t["code"]])
                        )
        except (ProjectError, IntegrityError):
            self.delete_project(pid, actor)
            raise
        self._log(pid, None, actor, "imported", tr("{0} công việc", len(new_ids)))
        return self._project_summary(self.project(pid))

    def csv(self, pid: int) -> str:
        board = self.board(pid)
        codes = {int(t["id"]): t["code"] for t in board["tasks"]}
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow([*CSV_COLUMNS, "done_pct", "late", "notes"])
        for t in board["tasks"]:
            writer.writerow(
                [
                    t["code"],
                    codes.get(int(t["parent_id"])) if t["parent_id"] is not None else "",
                    "  " * t["depth"] + t["title"],
                    t["assignee"],
                    t["priority"],
                    t["status"],
                    t["progress"],
                    t["labels"],
                    t["start_date"],
                    t["due_date"],
                    t["depends"],
                    t["done_pct"],
                    "1" if t["late"] else "",
                    plain(t["notes"]),
                ]
            )
        return "\ufeff" + out.getvalue()  # Excel reads the accents

    # ------------------------------------------------------------------ #
    # one person's tasks (their chat commands, their morning reminder)

    def my_tasks(self, username: str, include_done: bool = False) -> list[dict[str, Any]]:
        rows = self.db.rows(
            "SELECT t.id, t.project_id FROM pm_tasks t JOIN pm_projects p ON p.id=t.project_id "
            "WHERE t.assignee=? AND p.archived=0" + ("" if include_done else " AND t.status<>'done'"),
            (username,),
        )
        out = []
        for pid in {int(r["project_id"]) for r in rows}:
            project = self.project(pid)
            wanted = {int(r["id"]) for r in rows if int(r["project_id"]) == pid}
            for t in self._computed(self._rows(pid)):
                if int(t["id"]) in wanted:
                    out.append({**t, "project_name": project["name"]})
        out.sort(key=lambda t: (not t["late"], not t["due_soon"], t["due_date"] or "9999", t["code"]))
        return out

    def describe(self, t: dict[str, Any]) -> str:
        """A task in a chat message."""
        lines = [
            f"{t.get('icon') or '•'} {t['code']} · {t['title']}",
            tr(
                "{0} · {1}% · ưu tiên {2}",
                tr(STATUS_TEXT.get(t["status"], t["status"])),
                t["done_pct"],
                tr(PRIORITY_TEXT.get(t["priority"], t["priority"])),
            ),
        ]
        if t.get("project_name"):
            lines.append(tr("Dự án: {0}", t["project_name"]))
        if t["assignee"]:
            lines.append(tr("Người phụ trách: {0}", t["assignee"]))
        if t["start_date"] or t["due_date"]:
            lines.append(tr("Thời gian: {0} → {1}", t["start_date"] or "…", t["due_date"] or "…"))
        if t["late"]:
            lines.append(tr("⏰ Trễ hạn"))
        elif t["due_soon"]:
            lines.append(tr("⌛ Sắp đến hạn"))
        if t["waiting_for"]:
            lines.append(tr("Chờ việc: {0}", ", ".join(t["waiting_for"])))
        if t["checklist"]:
            lines += [f"{'☑' if i.get('done') else '☐'} {i['text']}" for i in t["checklist"]]
        if notes := plain(t["notes"]):
            lines.append(notes[:800])
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # telling people (their linked SimpleX chats)

    async def notify_user(self, username: str, text: Callable[[], str]) -> int:
        links = getattr(self.office, "staff_links", None)
        if links is None:
            return 0
        sent = 0
        for employee_id, cid, user in links.linked():
            if user.username != username:
                continue
            employee = self.office.employees.get(employee_id)
            if employee is None:
                continue
            with use_language(user.lang or None):
                message = text()
            try:
                await self.office.cluster.simplex_send(employee, cid, message)
                sent += 1
            except Exception:  # noqa: BLE001 - one unreachable phone must not stop the rest
                log.warning("projects: could not notify %s", username)
        return sent

    def _spawn_notify(self, username: str, text: Callable[[], str]) -> None:
        hub = getattr(self.office, "hub", None)
        if hub is not None:
            hub.spawn(self.notify_user(username, text))

    def _spawn_assigned(self, task: dict[str, Any], actor: str) -> None:
        def text() -> str:
            return tr("📋 {0} giao cho bạn việc mới:\n{1}", actor, self.describe(task))

        self._spawn_notify(task["assignee"], text)

    async def morning_digest(self, force: bool = False) -> int:
        """Once a day: each person's late tasks and those due soon, in their chat."""
        now = datetime.now().astimezone()
        day = now.date().isoformat()
        if not force and (now.hour < DIGEST_HOUR or self._digest_day == day):
            return 0
        self._digest_day = day
        links = getattr(self.office, "staff_links", None)
        if links is None:
            return 0
        sent = 0
        for username in sorted({user.username for _e, _c, user in links.linked()}):
            tasks = [t for t in self.my_tasks(username) if t["late"] or t["due_soon"]]
            if not tasks:
                continue

            def text(tasks: list[dict[str, Any]] = tasks) -> str:
                lines = [tr("☀️ Việc cần chú ý hôm nay:")]
                for t in tasks[:20]:
                    mark = tr("trễ hạn") if t["late"] else tr("sắp đến hạn")
                    lines.append(f"• {t['code']} {t['title']} — {t['due_date']} ({mark})")
                lines.append(tr("Xem: /'tasks'"))
                return "\n".join(lines)

            sent += await self.notify_user(username, text)
        return sent

    async def run(self, stopping: Any) -> None:
        import asyncio

        while not stopping.is_set():
            try:
                await self.morning_digest()
            except Exception:
                log.exception("projects: morning reminder failed")
            try:
                await asyncio.wait_for(stopping.wait(), timeout=600)
            except TimeoutError:
                pass

    # ------------------------------------------------------------------ #
    # who may do what (beyond seeing the page)

    @staticmethod
    def manages(user: User) -> bool:
        return user.role in ("admin", "manager")

    def may_delete_task(self, user: User, tid: int) -> bool:
        return self.manages(user) or self._row(tid)["created_by"] == user.username
