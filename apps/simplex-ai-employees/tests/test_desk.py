"""Help-desk features of the inbox: internal notes, open/closed, labels, assignment to
people and teams, triage rules, the SLA report and the AI thread summary."""

from __future__ import annotations

from datetime import datetime, timedelta

from test_channels import PASSWORD, H, env, settle, setup, ui  # noqa: F401 - fixtures

from ai_employees.desk import fold
from ai_employees.inbox import Inbox

from fakes import text

HOOK = {"X-Hook-Secret": "hook-secret-1"}


def test_notes_status_and_answer_times(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    conv = inbox.upsert("website", "v1", "Linh", "sales")
    t0 = datetime.now().astimezone() - timedelta(minutes=10)
    inbox.add(conv.id, "customer", "Shop ơi", "Linh", "m1", t0.isoformat(timespec="seconds"))
    c = inbox.conversation(conv.id)
    assert c.waiting_since == t0.isoformat(timespec="seconds") and c.status == "open"

    # a note changes nothing the customer, the AI or the counters see
    inbox.add(conv.id, "note", "Khách quen, hay mua sỉ", "Thu")
    c = inbox.conversation(conv.id)
    assert (c.last_sender, c.last_preview, c.unread) == ("customer", "Shop ơi", 1)
    assert [m["text"] for m in inbox.pending_customer_text(conv.id)] == ["Shop ơi"]
    first = inbox.messages(conv.id)[0]["id"]
    assert inbox.is_pending(conv.id, first)
    assert [m["sender"] for m in inbox.messages(conv.id, notes=False)] == ["customer"]

    # the answer stops the clock and is timed
    inbox.add(conv.id, "human", "Dạ em đây", "Thu")
    assert inbox.conversation(conv.id).waiting_since is None
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    since = (datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds")
    report = inbox.sla(since, now, 300)
    [row] = report["responders"]
    assert row["responder"] == "human" and row["author"] == "Thu" and row["answers"] == 1
    assert 590 <= row["avg_seconds"] <= 700 and row["within_target"] == 0

    # closing stops the clock; the customer writing again reopens
    inbox.add(conv.id, "customer", "Còn hàng không?", "Linh", "m2")
    assert inbox.sla(since, now, 300)["counts"]["waiting"] == 1
    inbox.set_status(conv.id, "closed")
    c = inbox.conversation(conv.id)
    assert (c.status, c.waiting_since, c.unread) == ("closed", None, 0)
    assert inbox.sla(since, now, 300)["counts"] == {"open": 0, "waiting": 0, "late": 0, "unassigned": 0}
    inbox.add(conv.id, "customer", "Alo?", "Linh", "m3")
    c = inbox.conversation(conv.id)
    assert c.status == "open" and c.waiting_since is not None

    # labels and assignment filters
    other = inbox.upsert("website", "v2", "Minh", "sales")
    inbox.set_labels(conv.id, ["VIP", "Khiếu nại", "VIP"])
    assert inbox.labels(conv.id) == ["Khiếu nại", "VIP"]
    inbox.set_assignee(other.id, "", "cskh")
    inbox.set_assignee(conv.id, "thu", "")
    assert [x.id for x in inbox.list(label="VIP")] == [conv.id]
    assert {x.id for x in inbox.list(assignee="thu", teams=["cskh"])} == {conv.id, other.id}
    assert [x.id for x in inbox.list(assignee="thu")] == [conv.id]
    assert [x.id for x in inbox.list(team="cskh")] == [other.id]
    assert inbox.list(assignee="-") == []
    inbox.rename_label("VIP", "Khách VIP")
    assert inbox.labels(conv.id) == ["Khiếu nại", "Khách VIP"]
    inbox.rename_label("Khiếu nại", None)
    assert inbox.labels(conv.id) == ["Khách VIP"]


def test_fold_ignores_accents_and_case():
    assert fold("KHIẾU NẠI đơn hàng") == "khieu nai don hang"
    assert fold("khieu nai") in fold("Em muốn Khiếu Nại")


async def test_desk_settings_triage_and_api(ui):  # noqa: F811
    client, office, llm, platforms = ui
    hub = office.hub
    assert (await client.post("/api/login", json={"password": PASSWORD}, headers=H)).status == 200
    r = await client.post(
        "/api/users",
        json={
            "username": "thu",
            "name": "Thu",
            "role": "agent",
            "password": "0123456789",
            "channels": ["website"],
        },
        headers=H,
    )
    assert r.status == 200

    async def put(section, value, status=200):
        r = await client.put(f"/api/desk/{section}", json={"value": value}, headers=H)
        assert r.status == status, await r.text()
        return await r.json()

    await put("labels", [{"name": "x", "color": "red"}], 400)
    await put("labels", [{"name": "VIP", "color": "#16a34a"}, {"name": "Khiếu nại", "color": "#dc2626"}])
    await put("teams", [{"id": "cskh", "name": "CSKH", "members": ["nobody"]}], 400)
    await put("teams", [{"id": "cskh", "name": "CSKH", "members": ["thu"]}])
    await put("canned", [{"title": "Chào", "text": "Dạ chào {name}, em hỗ trợ được gì ạ?"}])
    await put("rules", [{"name": "r", "labels": ["Không có"]}], 400)
    await put("rules", [{"name": "empty"}], 400)
    await put("sla_minutes", 0, 400)
    await put("sla_minutes", 5)
    desk = await put(
        "rules",
        [
            {
                "name": "Khiếu nại",
                "keywords": ["khieu nai", "hoàn tiền"],
                "labels": ["Khiếu nại"],
                "team": "cskh",
                "handoff": True,
            },
            {"name": "Web VIP", "channels": ["website"], "keywords": ["sỉ"], "labels": ["VIP"]},
        ],
    )
    assert [x["name"] for x in desk["rules"]] == ["Khiếu nại", "Web VIP"]

    # a complaint: labelled, queued for the team, and the AI stays silent
    conv = hub.push_inbound(
        "website",
        {
            "conversation_id": "v9",
            "customer_name": "Hà",
            "text": "Tôi muốn KHIẾU NẠI đơn 12",
            "message_id": "a",
        },
    )
    await settle(hub)
    c = hub.inbox.conversation(conv.id)
    assert (c.mode, c.team, c.assignee) == ("human", "cskh", "")
    assert hub.inbox.labels(conv.id) == ["Khiếu nại"] and llm.calls == []
    # an ordinary message is answered by the AI; a later match only adds labels
    llm.responses.append(text("Dạ có giá sỉ ạ."))
    other = hub.push_inbound(
        "website", {"conversation_id": "v10", "text": "Shop có bán sỉ không", "message_id": "b"}
    )
    await settle(hub)
    assert hub.inbox.labels(other.id) == ["VIP"] and platforms.sent[-1][1]["text"] == "Dạ có giá sỉ ạ."

    # agents cannot change the settings, and do not get the SLA report
    await client.post("/api/logout", json={}, headers=H)
    await client.post("/api/login", json={"username": "thu", "password": "0123456789"}, headers=H)
    assert (await client.put("/api/desk/labels", json={"value": []}, headers=H)).status == 403
    assert (await client.get("/api/inbox/sla")).status == 403
    meta = await (await client.get("/api/inbox/meta")).json()
    assert meta["my_teams"] == ["cskh"] and meta["canned"][0]["title"] == "Chào"
    assert {u["username"] for u in meta["users"]} == {"admin", "thu"}

    # "mine": the team's conversations
    mine = await (await client.get("/api/inbox?assignee=me&status=open")).json()
    assert [x["id"] for x in mine["conversations"]] == [conv.id]
    assert mine["conversations"][0]["labels"] == ["Khiếu nại"]
    base = f"/api/inbox/{conv.id}"
    d = await (
        await client.post(f"{base}/assignee", json={"assignee": "thu", "team": "cskh"}, headers=H)
    ).json()
    assert (d["conversation"]["assignee"], d["conversation"]["team"]) == ("thu", "cskh")
    assert (await client.post(f"{base}/assignee", json={"team": "nope"}, headers=H)).status == 400
    assert (await client.post(f"{base}/labels", json={"labels": ["Bịa"]}, headers=H)).status == 400
    d = await (await client.post(f"{base}/labels", json={"labels": ["VIP", "Khiếu nại"]}, headers=H)).json()
    assert d["conversation"]["labels"] == ["Khiếu nại", "VIP"]
    tagged = (await (await client.get("/api/inbox?label=VIP")).json())["conversations"]
    assert {x["id"] for x in tagged} == {conv.id, other.id}

    # an internal note: in the inbox, never sent, never in the bridge's feed
    sent = len(platforms.sent)
    d = await (await client.post(f"{base}/note", json={"text": "Đã gọi lại cho khách"}, headers=H)).json()
    assert d["messages"][-1]["sender"] == "note" and d["messages"][-1]["author"] == "Thu"
    assert len(platforms.sent) == sent and d["conversation"]["last_sender"] == "customer"
    feed = await (await client.get("/hooks/website/v9?after=0", headers=HOOK)).json()
    assert [m["sender"] for m in feed["messages"]] == ["customer"]

    # the AI briefing for staff reads the whole thread, notes included
    llm.responses.append(text("Khách cần: xử lý khiếu nại đơn 12\nViệc tiếp theo: gọi lại"))
    r = await client.post(f"{base}/summary", json={}, headers=H)
    assert (await r.json())["text"].startswith("Khách cần:")
    prompt = llm.calls[-1]["messages"][-1]["content"]
    prompt = prompt if isinstance(prompt, str) else " ".join(p.get("text", "") for p in prompt)
    assert "KHIẾU NẠI đơn 12" in prompt and "Ghi chú nội bộ Thu" in prompt
    assert not llm.calls[-1].get("tools")

    # reply, then close
    d = await (await client.post(f"{base}/reply", json={"text": "Dạ em xin lỗi chị"}, headers=H)).json()
    assert d["conversation"]["waiting_since"] is None
    d = await (await client.post(f"{base}/status", json={"status": "closed"}, headers=H)).json()
    assert d["conversation"]["status"] == "closed"
    assert (await (await client.get("/api/inbox?status=open&assignee=me")).json())["conversations"] == []

    # the SLA report, for admins
    await client.post("/api/logout", json={}, headers=H)
    await client.post("/api/login", json={"password": PASSWORD}, headers=H)
    sla = await (await client.get("/api/inbox/sla?hours=24")).json()
    assert sla["target_seconds"] == 300
    who = {(x["responder"], x["author"]): x["answers"] for x in sla["responders"]}
    assert who == {("ai", ""): 1, ("human", "Thu"): 1}
    assert sla["counts"]["open"] == 1 and sla["by_channel"]["website"]["answers"] == 2

    # renaming a label follows its conversations and rules; deleting one removes it
    await put(
        "labels",
        [{"name": "Khách VIP", "was": "VIP", "color": "#16a34a"}, {"name": "Khiếu nại", "color": "#dc2626"}],
    )
    assert hub.inbox.labels(conv.id) == ["Khiếu nại", "Khách VIP"]
    assert hub.desk.get()["rules"][1]["labels"] == ["Khách VIP"]
    desk = await put("labels", [{"name": "Khách VIP", "color": "#16a34a"}])
    assert hub.inbox.labels(conv.id) == ["Khách VIP"]
    assert [x["name"] for x in desk["rules"]] == ["Khiếu nại", "Web VIP"]
    assert desk["rules"][0]["labels"] == []
