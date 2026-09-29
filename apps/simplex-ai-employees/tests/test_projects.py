"""Projects and tasks: the tree, progress, dates, permissions, comments and files, the
JSON/CSV files, the web API and the staff's chat commands."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_channels import PASSWORD, H, settle

from ai_employees.projects import ProjectError, clean_html
from ai_employees.users import Users
from ai_employees.web import create_app

from fakes import ScriptedLLM, fake_chat, make_office


@pytest.fixture
def office(tmp_path):
    office = make_office(tmp_path, ScriptedLLM())
    users = Users(office.docs, "")
    users.add("lan", "Lan", "agent", "0123456789")
    users.add("quan", "Quân", "manager", "0123456789")
    users.add("thu", "Thu", "cashier", "0123456789")
    return office


def tree(pm, pid):
    board = pm.board(pid)
    return {t["title"]: t for t in board["tasks"]}, board["project"]


def test_tree_progress_and_dates(office):
    pm = office.projects
    p = pm.save_project(None, {"name": "Lumina Cosmetics JSC"}, "admin")
    pid = p["id"]
    mk = pm.create_task(pid, {"title": "Marketing"}, "admin")
    a = pm.create_task(pid, {"title": "Kế hoạch nội dung", "progress": 40}, "admin", parent_id=mk["id"])
    b = pm.create_task(pid, {"title": "Họp KPI", "status": "done"}, "admin", parent_id=mk["id"])
    c = pm.create_task(
        pid,
        {
            "title": "Mẫu hộp quà",
            "track_checklist": True,
            "checklist": [{"text": "25 mẫu", "done": True}, {"text": "6 hộp"}],
        },
        "admin",
        parent_id=mk["id"],
    )
    assert len(a["code"]) == 5 and a["code"] != b["code"]
    tasks, project = tree(pm, pid)
    assert tasks["Mẫu hộp quà"]["done_pct"] == 50  # follows its checklist
    assert tasks["Marketing"]["done_pct"] == round((40 + 100 + 50) / 3)
    assert (tasks["Marketing"]["leaf_done"], tasks["Marketing"]["leaf_total"]) == (1, 3)
    assert project["task_count"] == 3 and project["done_count"] == 1
    assert tasks["Marketing"]["children"] == [a["id"], b["id"], c["id"]]

    yesterday = (datetime.now().astimezone().date() - timedelta(days=1)).isoformat()
    soon = (datetime.now().astimezone().date() + timedelta(days=2)).isoformat()
    pm.update_task(a["id"], {"due_date": yesterday}, "lan")
    pm.update_task(c["id"], {"due_date": soon, "depends": a["code"]}, "lan")
    tasks, project = tree(pm, pid)
    assert tasks["Kế hoạch nội dung"]["late"] and tasks["Mẫu hộp quà"]["due_soon"]
    assert tasks["Mẫu hộp quà"]["waiting_for"] == [a["code"]]
    assert project["late_count"] == 1
    with pytest.raises(ProjectError, match="trước ngày bắt đầu"):
        pm.update_task(a["id"], {"start_date": "2030-01-10", "due_date": "2030-01-01"}, "lan")
    with pytest.raises(ProjectError, match="chính nó"):
        pm.update_task(a["id"], {"depends": a["code"]}, "lan")
    with pytest.raises(ProjectError, match="ZZZZZ"):
        pm.update_task(a["id"], {"depends": "ZZZZZ"}, "lan")

    # moving: up/down among siblings, to another branch, never into itself
    pm.move(c["id"], "admin", direction="up")
    assert tree(pm, pid)[0]["Marketing"]["children"] == [a["id"], c["id"], b["id"]]
    other = pm.create_task(pid, {"title": "Kế toán"}, "admin", after_id=mk["id"])
    pm.move(b["id"], "admin", parent_id=other["id"])
    tasks, _ = tree(pm, pid)
    assert tasks["Họp KPI"]["parent_id"] == other["id"] and tasks["Kế toán"]["done_pct"] == 100
    with pytest.raises(ProjectError, match="chính nó"):
        pm.move(mk["id"], "admin", parent_id=a["id"])
    # a sibling right after another
    mid = pm.create_task(pid, {"title": "Giữa"}, "admin", parent_id=mk["id"], after_id=a["id"])
    assert tree(pm, pid)[0]["Marketing"]["children"] == [a["id"], mid["id"], c["id"]]

    # deleting a branch takes its children (and their comments) with it
    pm.add_comment(a["id"], "xong phần 1", "lan")
    assert pm.delete_task(mk["id"], "admin") == 4
    tasks, _ = tree(pm, pid)
    assert set(tasks) == {"Kế toán", "Họp KPI"}
    assert office.hub.inbox.db.rows("SELECT * FROM pm_comments") == []
    actions = [e["action"] for e in pm.log(pid)]
    assert {"project_created", "created", "updated", "moved", "commented", "deleted"} <= set(actions)


def test_notes_are_cleaned_and_fields_checked(office):
    pm = office.projects
    pid = pm.save_project(None, {"name": "P"}, "admin")["id"]
    t = pm.create_task(pid, {"title": "  Việc   một "}, "admin")
    assert t["title"] == "Việc một"
    dirty = (
        '<p onclick="x()">Mục tiêu <b>rõ</b><script>alert(1)</script></p>'
        '<a href="javascript:alert(1)">x</a><a href="https://ok.vn/a?b=1&c=2">link</a><img src=x onerror=y>'
    )
    notes = pm.update_task(t["id"], {"notes": dirty}, "admin")["notes"]
    assert (
        "script" not in notes and "onclick" not in notes and "javascript" not in notes and "<img" not in notes
    )
    assert "<b>rõ</b>" in notes and 'href="https://ok.vn/a?b=1&amp;c=2"' in notes
    assert clean_html("<ul><li>a<li>b</ul>") == "<ul><li>a<li>b</li></li></ul>"
    for bad in (
        {"priority": "whenever"},
        {"status": "lost"},
        {"color": "red"},
        {"due_date": "tomorrow"},
        {"avatar": "data:image/svg+xml;base64,PHN2Zz4="},
        {"links": "javascript:alert(1)"},
        {"title": "   "},
        {"checklist": "one"},
    ):
        with pytest.raises(ProjectError):
            pm.update_task(t["id"], bad, "admin")
    ok = pm.update_task(
        t["id"],
        {
            "avatar": "data:image/png;base64,iVBORw0KGgo=",
            "links": "https://a.vn\n\nhttp://b.vn",
            "labels": "a, ,b",
        },
        "admin",
    )
    assert ok["links_list"] == ["https://a.vn", "http://b.vn"] and ok["labels_list"] == ["a", "b"]


def test_export_import_and_csv(office):
    pm = office.projects
    pid = pm.save_project(None, {"name": "Gốc", "color": "#ffeecc"}, "admin")["id"]
    root = pm.create_task(pid, {"title": "Nhánh", "icon": "⭐"}, "admin")
    kid = pm.create_task(
        pid, {"title": "Lá", "assignee": "lan", "checklist": [{"text": "x"}]}, "admin", parent_id=root["id"]
    )
    pm.update_task(root["id"], {"depends": kid["code"]}, "admin")
    pm.add_comment(kid["id"], "ghi chú", "lan")
    data = pm.export(pid)
    assert data["format"] == "ai-employees-project" and len(data["tasks"]) == 2
    copy = pm.import_project(data, "quan")
    tasks, project = tree(pm, copy["id"])
    assert project["name"] == "Gốc" and set(tasks) == {"Nhánh", "Lá"}
    assert tasks["Lá"]["parent_id"] == tasks["Nhánh"]["id"] and tasks["Lá"]["code"] != kid["code"]
    assert tasks["Nhánh"]["depends_list"] == [tasks["Lá"]["code"]]  # the new code
    assert pm.comments(tasks["Lá"]["id"])[0]["text"] == "ghi chú"
    with pytest.raises(ProjectError):
        pm.import_project({"tasks": []}, "quan")
    text = pm.csv(pid)
    assert text.startswith("﻿code,parent,title") and "  Lá" in text and kid["code"] in text


async def test_web_api_roles_files_and_my_tasks(office):
    client = TestClient(TestServer(create_app(office, PASSWORD)))
    await client.start_server()
    try:

        async def call(method, path, body=None, status=200, **kw):
            r = await client.request(method, path, json=body, headers=H, **kw)
            data = await r.json() if r.content_type == "application/json" else await r.read()
            assert r.status == status, data
            return data

        async def login(username, password="0123456789"):
            await call("POST", "/api/logout", {})
            await call("POST", "/api/login", {"username": username, "password": password})

        await call("POST", "/api/login", {"password": PASSWORD})
        p = await call("POST", "/api/pm/projects", {"name": "Lumina"})
        top = await call("POST", f"/api/pm/projects/{p['id']}/tasks", {"title": "Kế toán"})
        task = await call(
            "POST",
            f"/api/pm/projects/{p['id']}/tasks",
            {"title": "Rà soát chi phí", "parent_id": top["id"], "assignee": "lan"},
        )
        await call("PATCH", f"/api/pm/tasks/{task['id']}", {"status": "lost"}, status=400)
        board = await call("GET", f"/api/pm/projects/{p['id']}")
        assert [t["title"] for t in board["tasks"]] == ["Kế toán", "Rà soát chi phí"]
        assert {"lan", "quan"} <= {x["username"] for x in board["people"]} and board["manager"]

        # a cashier works on tasks, but projects are for managers and admins
        await login("thu")
        await call("GET", f"/api/pm/projects/{p['id']}")
        await call("POST", "/api/pm/projects", {"name": "X"}, status=403)
        await call("DELETE", f"/api/pm/projects/{p['id']}", status=403)
        mine = await call("POST", f"/api/pm/projects/{p['id']}/tasks", {"title": "Việc của Thu"})
        await call("DELETE", f"/api/pm/tasks/{task['id']}", status=403)  # not theirs
        await call("DELETE", f"/api/pm/tasks/{mine['id']}")

        # the assignee: their tasks, comments, files
        await login("lan")
        assert [t["code"] for t in (await call("GET", "/api/pm/my"))["tasks"]] == [task["code"]]
        await call("PATCH", f"/api/pm/tasks/{task['id']}", {"progress": 70, "status": "doing"})
        c = await call("POST", f"/api/pm/tasks/{task['id']}/comments", {"text": "đang làm"})
        r = await client.put(
            f"/api/pm/tasks/{task['id']}/files?name=bao-cao.csv",
            data=b"a,b\n1,2\n",
            headers={**H, "Content-Type": "text/csv"},
        )
        f = await r.json()
        assert r.status == 200 and f["size"] == 8
        r = await client.get(f"/api/pm/files/{f['id']}")
        assert await r.read() == b"a,b\n1,2\n" and "attachment" in r.headers["Content-Disposition"]
        assert r.headers["Content-Type"] == "application/octet-stream"
        big = await client.put(
            f"/api/pm/tasks/{task['id']}/files?name=x", data=b"x" * (10 * 1024 * 1024 + 1), headers=H
        )
        assert big.status == 413
        await login("quan")
        await call("DELETE", f"/api/pm/comments/{c['id']}")  # a manager may
        board = await call("GET", f"/api/pm/projects/{p['id']}")
        done = next(t for t in board["tasks"] if t["id"] == top["id"])
        assert done["done_pct"] == 70 and done["file_count"] == 0 and board["tasks"][1]["file_count"] == 1
        r = await client.get(f"/api/pm/projects/{p['id']}/export.json")
        assert r.status == 200 and "attachment" in r.headers["Content-Disposition"]
        copy = await call("POST", "/api/pm/import", await r.json())
        assert copy["task_count"] == 1
        assert len((await call("GET", f"/api/pm/projects/{p['id']}/log"))["log"]) >= 5
        await call("PATCH", f"/api/pm/projects/{copy['id']}", {"archived": True})
        assert [x["id"] for x in (await call("GET", "/api/pm/projects?archived=1"))["projects"]] == [
            copy["id"]
        ]
        await call("DELETE", f"/api/pm/projects/{copy['id']}")
        await call("GET", f"/api/pm/projects/{copy['id']}", status=404)
        await call("GET", "/api/pm/projects/999999999999999999999", status=404)
    finally:
        await client.close()


async def test_chat_commands_assignment_and_morning_reminder(office):
    sales = office.employees["sales"]
    chat = fake_chat(sales)
    pm = office.projects
    code = office.staff_links.new_code("lan")
    await sales.staff.handle(40, "link", code.lower())
    chat.sent.clear()
    pid = pm.save_project(None, {"name": "Lumina"}, "admin")["id"]
    late = (datetime.now().astimezone().date() - timedelta(days=2)).isoformat()
    t = pm.create_task(
        pid, {"title": "Xác nhận sản phẩm đăng web", "assignee": "lan", "due_date": late}, "quan"
    )
    await settle(office.hub)
    assert any("giao cho bạn" in text and t["code"] in text for _cid, text in chat.sent)

    listing = await sales.staff.handle(40, "tasks", "")
    assert t["code"] in listing and "trễ hạn" in listing
    detail = await sales.staff.handle(40, "task", t["code"].lower())
    assert "Xác nhận sản phẩm đăng web" in detail and "Trễ hạn" in detail
    assert "50%" in await sales.staff.handle(40, "progress", f"{t['code']} 50")
    assert pm.task(t["id"])["progress"] == 50
    assert "Không tìm thấy" in await sales.staff.handle(40, "task", "NOPE1")
    assert "Cú pháp" in await sales.staff.handle(40, "progress", f"{t['code']} lots")
    done = await sales.staff.handle(40, "taskdone", t["code"])
    assert "Hoàn thành" in done and pm.task(t["id"])["status"] == "done"

    other = pm.create_task(pid, {"title": "Việc khác", "due_date": late, "assignee": "lan"}, "lan")
    chat.sent.clear()
    assert await pm.morning_digest(force=True) == 1
    assert other["code"] in chat.sent[-1][1] and t["code"] not in chat.sent[-1][1]
    assert await pm.morning_digest() == 0  # once a day
