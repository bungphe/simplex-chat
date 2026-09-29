"""Operations: backups and restore, rotation, /healthz, /metrics and the watchdog."""

from __future__ import annotations

import os
import sqlite3
import sys
import tarfile
import time
from types import SimpleNamespace as NS

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from ai_employees import ops as ops_mod
from ai_employees.db import Database, DocStore
from ai_employees.ops import Backups
from ai_employees.storefront import create_shop_app

from fakes import ScriptedLLM, fake_chat, make_office


@pytest.fixture
def office(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_BACKUP_DIR", raising=False)
    monkeypatch.delenv("AI_BACKUP_COMMAND", raising=False)
    monkeypatch.delenv("AI_METRICS_TOKEN", raising=False)
    office = make_office(tmp_path, ScriptedLLM())
    # what SimpleX would have created for the "sales" employee (db: <tmp>/db/sales)
    for name in ("sales_chat.db", "sales_agent.db"):
        with sqlite3.connect(tmp_path / "db" / name) as c:
            c.execute("CREATE TABLE t (x INTEGER)")
            c.execute("INSERT INTO t VALUES (42)")
    return office


def postgres(office) -> bool:
    return office.config.database_url is not None


# --------------------------------------------------------------------------- #
# Backups
# --------------------------------------------------------------------------- #


async def test_backup_archives_the_databases_and_restore_brings_them_back(office, tmp_path):
    office.docs.update("probe", lambda d: d.update(answer=1), {})
    (tmp_path / "state" / "notes.json").write_text("{}")
    backups = office.ops.backups
    path = await backups.run()
    assert path.parent == tmp_path / "state" / "backups" and path.name.startswith("backup-")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    with tarfile.open(path) as tar:
        names = set(tar.getnames())
    assert {
        "manifest.json",
        "state/notes.json",
        "employees/sales/sales_chat.db",
        "employees/sales/sales_agent.db",
    } <= names
    if postgres(office):
        assert "postgres.dump" in names
    else:
        assert "state/office.sqlite" in names and "state/inbox.db" in names
    assert not any(n.startswith("state/backups/") for n in names)  # never itself
    st = backups.status()
    assert st["last_ok"] and st["last_file"] == str(path) and st["size"] == path.stat().st_size
    assert st["last_error"] is None
    # the archive holds a consistent copy: it opens as a database with the row in it
    with tarfile.open(path) as tar, tar.extractfile("employees/sales/sales_chat.db") as f:
        (tmp_path / "check.db").write_bytes(f.read())
    with sqlite3.connect(tmp_path / "check.db") as c:
        assert c.execute("SELECT x FROM t").fetchone() == (42,)

    if postgres(office):
        return  # pg_restore would replace the shared test schema
    # things change after the backup...
    office.docs.update("probe", lambda d: d.update(answer=2), {})
    (tmp_path / "db" / "sales_chat.db").unlink()
    (tmp_path / "state" / "notes.json").write_text("changed")
    # ...and the restore puts the archive back, keeping the current state next to it
    notes = Backups(office.config).restore(path)
    assert any("before-restore" in n for n in notes)
    moved = [p for p in tmp_path.iterdir() if p.name.startswith("state.before-restore-")]
    assert len(moved) == 1 and (moved[0] / "notes.json").read_text() == "changed"
    assert (tmp_path / "state" / "notes.json").read_text() == "{}"
    with sqlite3.connect(tmp_path / "db" / "sales_chat.db") as c:
        assert c.execute("SELECT x FROM t").fetchone() == (42,)
    fresh = DocStore(Database(str(tmp_path / "state" / "office.sqlite")))
    assert fresh.get("probe") == {"answer": 1}


async def test_backup_runs_the_after_command_with_the_file(office, tmp_path):
    copy = tmp_path / "offsite"
    copy.mkdir()
    script = "import shutil, sys; shutil.copy(sys.argv[1], sys.argv[2])"
    backups = Backups(
        office.config, office.docs, after_command=f'{sys.executable} -c "{script}" {{file}} {copy}'
    )
    path = await backups.run()
    assert (copy / path.name).exists()
    assert backups.status()["upload_error"] is None
    # a failing command is recorded but the archive stays
    failing = Backups(
        office.config, office.docs, after_command=f"{sys.executable} -c 'import sys; sys.exit(3)'"
    )
    path2 = await failing.run()
    assert path2.exists() and "exited 3" in failing.status()["last_error"]


def test_rotation_keeps_the_newest_and_drops_old_or_surplus(office, tmp_path):
    backups = Backups(office.config, backup_dir=tmp_path / "bk", keep_days=14, keep_count=3)
    backups.dir.mkdir()
    files = [backups.dir / f"backup-2026090{i}-030000.tar.gz" for i in range(1, 6)]
    for f in files:
        f.write_bytes(b"x")
    old = time.time() - 20 * 86400
    os.utime(files[2], (old, old))  # within the newest 3 by name, but older than keep_days
    removed = backups.rotate()
    assert set(removed) == {files[0], files[1], files[2]}
    assert [f for f in files if f.exists()] == [files[3], files[4]]
    # the newest is never removed, however old
    os.utime(files[4], (old, old))
    files[3].unlink()
    assert backups.rotate() == [] and files[4].exists()


def test_nightly_schedule_is_once_a_day_from_the_hour_on(office):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    backups = Backups(office.config, office.docs, hour=3)
    day = datetime(2026, 9, 29, 2, 59, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    assert not backups.due(day)
    assert backups.due(day.replace(hour=3))
    assert backups.due(day.replace(hour=15))  # a late start still gets its backup
    backups._set_status(last_day="2026-09-29", last_attempt="2026-09-29T03:00:00+07:00")
    assert not backups.due(day.replace(hour=15))
    assert backups.due(day.replace(day=30, hour=3))
    disabled = Backups(office.config, office.docs, hour=3, enabled=False)
    assert not disabled.due(day.replace(hour=4))


def test_restore_refuses_unsafe_archives(office, tmp_path):
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        info = tarfile.TarInfo("state/../../etc/passwd")
        info.size = 0
        tar.addfile(info)
    with pytest.raises(ops_mod.BackupError):
        Backups(office.config).restore(bad)
    assert (tmp_path / "state").exists()  # nothing was touched


# --------------------------------------------------------------------------- #
# /healthz and /metrics
# --------------------------------------------------------------------------- #


@pytest.fixture
async def admin(office):
    app = web.Application()
    office.ops.add_routes(app.router)
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client
    await client.close()


async def test_healthz_reports_the_database(admin, office, monkeypatch):
    r = await admin.get("/healthz")
    body = await r.json()
    assert r.status == 200 and body["ok"] and body["db"] == "ok" and body["employees"] == 2
    assert body["version"] and body["uptime_s"] >= 0

    def broken(*_a, **_k):
        raise RuntimeError("database is closed")

    monkeypatch.setattr(office.office_db, "row", broken)
    r = await admin.get("/healthz")
    assert r.status == 503 and (await r.json())["db"] == "error"


async def test_the_web_shop_has_healthz_too(office):
    app = create_shop_app(office)
    office.ops.health_routes(app.router)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        r = await client.get("/healthz")
        assert r.status == 200 and (await r.json())["ok"]
    finally:
        await client.close()


async def test_metrics_need_the_token(admin, office, monkeypatch):
    assert (await admin.get("/metrics")).status == 404  # unset: the route does not exist
    monkeypatch.setenv("AI_METRICS_TOKEN", "metrics-secret")
    assert (await admin.get("/metrics")).status == 401
    assert (await admin.get("/metrics", headers={"Authorization": "Bearer wrong"})).status == 401
    office.runlog.append(employee="sales", kind="reply", status="ok", tokens_in=1, tokens_out=1)
    office.runlog.append(employee="sales", kind="reply", status="error")
    r = await admin.get("/metrics", headers={"Authorization": "Bearer metrics-secret"})
    assert r.status == 200 and r.headers["Content-Type"].startswith("text/plain")
    text = await r.text()
    assert "# TYPE aie_uptime_seconds gauge" in text
    assert 'aie_runs_15m{status="ok"} 1' in text and 'aie_runs_15m{status="error"} 1' in text
    assert "aie_model_errors_15m 1" in text
    assert "aie_inbox_unread 0" in text and "aie_orders_open 0" in text
    assert "aie_employees 2" in text and "aie_up 1" in text
    assert "aie_disk_free_bytes " in text and "aie_db_size_bytes " in text
    assert "aie_backup_ok 0" in text and "aie_backup_age_seconds" not in text  # none yet
    await office.ops.backups.run()
    text = await (await admin.get("/metrics", headers={"Authorization": "Bearer metrics-secret"})).text()
    assert (
        "aie_backup_ok 1" in text and "aie_backup_age_seconds " in text and "aie_backup_size_bytes " in text
    )


# --------------------------------------------------------------------------- #
# Watchdog
# --------------------------------------------------------------------------- #


async def test_watchdog_alerts_once_per_six_hours_and_reports_recovery(office, monkeypatch):
    chat = fake_chat(office.employees["sales"])
    office.employees["sales"].state.add_admin(7)
    clock = [1_000_000.0]
    wd = office.ops.watchdog
    wd.now = lambda: clock[0]
    tb = 1024**3
    monkeypatch.setattr(
        ops_mod.shutil, "disk_usage", lambda _p: NS(total=100 * tb, used=99 * tb, free=1 * tb)
    )

    sent = await wd.check()
    assert len(sent) == 1 and "Ổ đĩa sắp đầy" in sent[0] and "1.0 GB" in sent[0]
    assert chat.sent == [(7, sent[0])]
    assert "disk" in office.docs.get("ops_alerts", {})
    # still low: silence for six hours
    assert await wd.check() == []
    clock[0] += 3 * 3600
    assert await wd.check() == []
    clock[0] += 3 * 3600 + 1
    again = await wd.check()
    assert len(again) == 1 and "Ổ đĩa sắp đầy" in again[0]
    # fixed: one recovery message, then nothing
    monkeypatch.setattr(
        ops_mod.shutil, "disk_usage", lambda _p: NS(total=100 * tb, used=10 * tb, free=90 * tb)
    )
    ok = await wd.check()
    assert len(ok) == 1 and "Đã ổn lại" in ok[0] and "dung lượng ổ đĩa" in ok[0]
    assert await wd.check() == []
    assert len(chat.sent) == 3 and "disk" not in office.docs.get("ops_alerts", {})


async def test_watchdog_sees_channel_errors_and_model_errors(office, monkeypatch):
    chat = fake_chat(office.employees["sales"])
    office.employees["sales"].state.add_admin(7)
    tb = 1024**3
    monkeypatch.setattr(
        ops_mod.shutil, "disk_usage", lambda _p: NS(total=100 * tb, used=10 * tb, free=90 * tb)
    )
    wd = office.ops.watchdog
    assert await wd.check() == []
    for _ in range(6):
        office.runlog.append(employee="sales", kind="reply", status="error")
    office.hub.channels["zalo-test"] = NS(id="zalo-test")
    office.hub.inbox.set_channel_state("zalo-test", last_error="token expired")
    sent = await wd.check()
    assert len(sent) == 2
    assert any("6 lỗi gọi model AI" in m for m in sent) and any(
        "zalo-test" in m and "token expired" in m for m in sent
    )
    office.hub.inbox.set_channel_state("zalo-test", last_error=None)
    recovered = await wd.check()
    assert len(recovered) == 1 and "kênh zalo-test" in recovered[0]
    assert len(chat.sent) == 3


async def test_stale_backup_is_reported_only_after_36_hours(office, monkeypatch):
    fake_chat(office.employees["sales"])
    office.employees["sales"].state.add_admin(7)
    tb = 1024**3
    monkeypatch.setattr(
        ops_mod.shutil, "disk_usage", lambda _p: NS(total=100 * tb, used=10 * tb, free=90 * tb)
    )
    wd = office.ops.watchdog
    assert await wd.check() == []  # a fresh office: the clock starts now
    office.ops.backups._set_status(first_seen="2020-01-01T00:00:00+00:00")
    sent = await wd.check()
    assert len(sent) == 1 and "sao lưu" in sent[0]
    office.ops.backups.enabled = False  # backups turned off: not a condition any more
    cleared = await wd.check()
    assert len(cleared) == 1 and "Đã ổn lại" in cleared[0]
    office.ops.backups.enabled = True
    await office.ops.backups.run()  # a fresh backup: nothing to report
    assert await wd.check() == []
