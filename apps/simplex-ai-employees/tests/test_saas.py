"""The SaaS control plane: sign-up and provisioning, portal and console, billing lifecycle,
plan limits inside the product, provisioner file generation and command safety."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from ai_employees.config import ConfigError, parse_config
from ai_employees.db import Database, DocStore
from ai_employees.saas import web as saas_web
from ai_employees.saas.billing import Billing, add_month
from ai_employees.saas.config import parse_saas_config
from ai_employees.saas.notify import Mailer, Notifier
from ai_employees.saas.provisioner import DockerComposeBackend, FakeBackend, ProvisionError
from ai_employees.saas.service import Service, suggest_slug
from ai_employees.saas.store import SaasStore
from ai_employees.saas.web import CSRF, CSRF_HEADER, CSRF_VALUE, OPERATOR, TENANT, create_app
from ai_employees.users import Users

RAW = {
    "public_url": "http://127.0.0.1:8090",
    "base_domain": "aie.test",
    "backend": "fake",
    "trial_days": 14,
    "grace_days": 7,
    "delete_after_days": 30,
    "operators": [{"username": "ops", "password": "operator-secret-pw"}],
    "bank": {"Ngân hàng": "VCB", "Số tài khoản": "0011", "Chủ tài khoản": "CTY AIE"},
    "plans": {
        "starter": {
            "name": "Khởi nghiệp",
            "price_month": 490000,
            "limits": {"users": 2, "employees": 1, "channels": 2, "storage_mb": 2000},
        },
        "pro": {
            "name": "Chuyên nghiệp",
            "price_month": 990000,
            "limits": {"users": 10, "employees": 3, "channels": 5},
        },
    },
}
SIGNUP = {
    "shop_name": "Tiệm hoa Mai",
    "owner_name": "Mai",
    "email": "mai@example.com",
    "phone": "0901234567",
    "password": "hoa-mai-2026!",
    "slug": "tiem-hoa-mai",
    "lang": "vi",
}
JSON_H = {CSRF_HEADER: CSRF_VALUE}


class Clock:
    def __init__(self) -> None:
        self.today = date(2026, 3, 1)

    def __call__(self) -> date:
        return self.today


@pytest.fixture
def world(tmp_path):
    cfg = parse_saas_config(
        {**RAW, "database_url": str(tmp_path / "saas.sqlite"), "tenants_dir": str(tmp_path / "t")}, tmp_path
    )
    backend = FakeBackend()
    mailer = Mailer(None)
    store = SaasStore(Database(cfg.database_url))
    service = Service(cfg, store, backend, Notifier(cfg, mailer))
    service.retry_delay = 0
    clock = Clock()
    service.today = clock
    return cfg, service, Billing(service), backend, mailer, clock


@pytest.fixture
async def client(world):
    cfg, service, billing, *_ = world
    c = TestClient(TestServer(create_app(cfg, service, billing)))
    await c.start_server()
    yield c
    await service.drain()
    await c.close()


def cookie(client: TestClient, name: str) -> str:
    jar = client.session.cookie_jar.filter_cookies(client.make_url("/"))
    return jar[name].value if name in jar else ""


async def csrf(client: TestClient) -> str:
    if not cookie(client, CSRF):
        await client.get("/health")
    return cookie(client, CSRF)


async def post(client: TestClient, path: str, **data):
    return await client.post(path, data={**data, "csrf": await csrf(client)}, allow_redirects=False)


def last_code(mailer: Mailer) -> str:
    m = re.search(r"\b(\d{6})\b", mailer.sent[-1]["subject"])
    assert m
    return m.group(1)


async def signed_up(client: TestClient, world, **over) -> dict:
    """Sign up and verify; returns the tenant. The client keeps the tenant session."""
    _cfg, service, _b, _backend, mailer, _clock = world
    data = {**SIGNUP, **over}
    r = await post(client, "/signup", **data)
    assert r.status == 303, await r.text()
    r = await post(client, "/verify", email=data["email"], code=last_code(mailer))
    assert r.status == 303 and r.headers["Location"] == "/portal"
    await service.drain()
    return service.store.tenant_by("email", data["email"])


# --------------------------------------------------------------------------- #
# Sign-up


async def test_signup_verify_provision_and_password_shown_once(client, world):
    _cfg, service, _b, backend, mailer, clock = world
    r = await client.get("/")
    assert r.status == 200 and "default-src 'self'" in r.headers["Content-Security-Policy"]
    page = await r.text()
    assert "490.000 VND" in page and "Dùng thử miễn phí 14 ngày" in page and "<script" not in page
    assert (await client.get("/?lang=en")).status == 200

    r = await post(client, "/signup", **SIGNUP)
    assert r.status == 303 and r.headers["Location"].startswith("/verify?email=")
    pending = service.store.tenant_by("slug", "tiem-hoa-mai")
    assert pending["status"] == "pending_email" and mailer.sent[-1]["to"] == "mai@example.com"
    assert not backend.calls  # nothing runs before the email is verified

    r = await post(client, "/verify", email=SIGNUP["email"], code="000000")
    assert r.status == 403
    r = await post(client, "/verify", email=SIGNUP["email"], code=last_code(mailer))
    assert r.status == 303 and cookie(client, TENANT)
    await service.drain()

    t = service.store.tenant_by("slug", "tiem-hoa-mai")
    assert t["status"] == "trial" and t["trial_ends"] == (clock.today + timedelta(days=14)).isoformat()
    assert t["provision_state"] == "ready" and t["admin_port"] == 20000 + 2 * t["id"]
    assert [c[0] for c in backend.calls] == ["create"]
    secrets = backend.calls[0][2]
    assert set(secrets) >= {"AI_ADMIN_PASSWORD", "AI_ADMIN_TOKEN"}
    assert mailer.sent[-1]["subject"] == "Cửa hàng Tiệm hoa Mai đã sẵn sàng"
    assert "https://tiem-hoa-mai.aie.test" in mailer.sent[-1]["text"]

    portal = await (await client.get("/portal")).text()
    assert secrets["AI_ADMIN_PASSWORD"] in portal and "https://tiem-hoa-mai-shop.aie.test" in portal
    assert secrets["AI_ADMIN_PASSWORD"] not in await (await client.get("/portal")).text()  # once
    # Caddy asks before issuing a certificate on demand
    for host, status in (
        ("tiem-hoa-mai.aie.test", 200),
        ("tiem-hoa-mai-shop.aie.test", 200),
        ("x.aie.test", 404),
        ("tiem-hoa-mai.evil.test", 404),
    ):
        assert (await client.get("/caddy-ask", params={"domain": host})).status == status, host


async def test_provisioning_retries(client, world):
    _cfg, service, _b, backend, _mailer, _clock = world
    backend.fail_first = 2
    t = await signed_up(client, world)
    assert t["provision_state"] == "ready"
    assert [c[0] for c in backend.calls] == ["create", "create", "create"]
    assert any(e["action"] == "provision_failed" for e in service.store.events(t["id"]))


async def test_signup_validation_and_uniqueness(client, world, monkeypatch):
    _cfg, service, *_ = world
    monkeypatch.setitem(saas_web.LIMITS, "signup", (100, 3600))  # 5 per hour otherwise (tested below)
    await signed_up(client, world)
    second = TestClient(TestServer(create_app(*world[:3])))
    await second.start_server()
    try:
        r = await post(second, "/signup", **{**SIGNUP, "email": "other@example.com"})
        assert r.status == 400 and "đã có người dùng" in await r.text()
        r = await post(second, "/signup", **{**SIGNUP, "slug": "khac"})
        assert r.status == 400 and "đã đăng ký" in await r.text()
        for bad in (
            {"slug": "Ab"},
            {"slug": "a_b"},
            {"slug": "www"},
            {"email": "no-at"},
            {"password": "short"},
            {"shop_name": ""},
        ):
            r = await post(second, "/signup", **{**SIGNUP, "email": "x@example.com", "slug": "xshop", **bad})
            assert r.status == 400, bad
        # JSON needs the header, forms need the token
        r = await second.post("/signup", json={**SIGNUP, "email": "j@example.com", "slug": "jshop"})
        assert r.status == 403
        r = await second.post("/signup", data={**SIGNUP, "email": "j@example.com", "slug": "jshop"})
        assert r.status == 403
        r = await second.post(
            "/signup", json={**SIGNUP, "email": "j@example.com", "slug": "jshop"}, headers=JSON_H
        )
        assert r.status == 200 and (await r.json())["slug"] == "jshop"
        # an unverified sign-up does not hold its slug against a new one
        r = await second.post(
            "/signup", json={**SIGNUP, "email": "k@example.com", "slug": "jshop"}, headers=JSON_H
        )
        assert r.status == 200 and service.store.tenant_by("email", "j@example.com") is None
    finally:
        await second.close()
    assert suggest_slug("Tiệm Hoa Đà Lạt") == "tiem-hoa-da-lat" and len(suggest_slug("A")) >= 3


async def test_verify_rate_limit_and_code_attempts(client, world):
    _cfg, _service, _b, _backend, mailer, _clock = world
    await post(client, "/signup", **SIGNUP)
    code = last_code(mailer)
    statuses = []
    for _ in range(6):
        statuses.append((await post(client, "/verify", email=SIGNUP["email"], code="123456")).status)
    assert statuses == [403] * 6
    r = await post(client, "/verify", email=SIGNUP["email"], code=code)
    assert r.status == 403  # too many wrong attempts burnt the code
    for _ in range(3):
        await post(client, "/verify", email=SIGNUP["email"], code=code)
    r = await post(client, "/verify", email=SIGNUP["email"], code=code)
    assert r.status == 429  # 10 checks per address per window
    assert (await post(client, "/verify/resend", email=SIGNUP["email"])).status == 429


# --------------------------------------------------------------------------- #
# Portal and console


async def op_login(client: TestClient, password: str = "operator-secret-pw"):
    return await post(client, "/console/login", username="ops", password=password)


async def test_portal_invoice_flow_and_console_confirm(client, world):
    cfg, service, billing, backend, mailer, clock = world
    t = await signed_up(client, world)
    await post(client, "/portal/logout")
    assert (await client.get("/portal", allow_redirects=False)).status == 303
    assert (
        await post(client, "/portal/login", email=SIGNUP["email"], password="wrong-password")
    ).status == 401
    r = await post(client, "/portal/login", email=SIGNUP["email"], password=SIGNUP["password"])
    assert r.status == 303 and cookie(client, TENANT)

    # day 10 of the trial: reminder and the first invoice
    clock.today += timedelta(days=10)
    counts = await billing.daily()
    assert counts["reminders"] == 1
    inv = service.store.invoices(t["id"])[0]
    assert inv["status"] == "due" and inv["amount"] == 490000 and inv["period_start"] == t["trial_ends"]
    assert inv["period_end"] == add_month(date.fromisoformat(t["trial_ends"])).isoformat()
    assert "Còn 4 ngày dùng thử" in [m["subject"] for m in mailer.sent]
    assert await billing.daily() == {**counts, "reminders": 0}  # idempotent

    page = await (await client.get(f"/portal/invoices/{inv['id']}")).text()
    assert f"SAAS-{inv['id']}" in page and "0011" in page and "Đã chuyển khoản" in page
    r = await post(client, f"/portal/invoices/{inv['id']}/transferred")
    assert r.status == 303 and service.store.invoice(inv["id"])["status"] == "awaiting_confirmation"
    assert (await post(client, f"/portal/invoices/{inv['id']}/transferred")).status == 409
    # another tenant's invoice is out of reach
    other = TestClient(TestServer(create_app(cfg, service, billing)))
    await other.start_server()
    try:
        await signed_up(other, world, email="b@example.com", slug="shop-b")
        assert (await other.get(f"/portal/invoices/{inv['id']}")).status == 404
    finally:
        await other.close()

    # the operator confirms the transfer
    console = TestClient(TestServer(create_app(cfg, service, billing)))
    await console.start_server()
    try:
        assert (await console.get("/console", allow_redirects=False)).status == 303
        assert (await console.post("/console/daily", json={}, headers=JSON_H)).status == 401
        assert (await op_login(console, "nope")).status == 401
        assert (await op_login(console)).status == 303 and cookie(console, OPERATOR)
        home = await (await console.get("/console")).text()
        assert "tiem-hoa-mai" in home and "Đã báo chuyển khoản" in home
        r = await post(console, f"/console/invoices/{inv['id']}/confirm", ref="FT2026")
        assert r.status == 303
        detail = await (await console.get(f"/console/tenants/{t['id']}")).text()
        assert "Đã thanh toán" in detail and "invoice_paid" in detail
    finally:
        await console.close()
    inv = service.store.invoice(inv["id"])
    assert inv["status"] == "paid" and inv["ref"] == "FT2026" and inv["gateway"] == "bank_transfer"
    t = service.store.tenant(t["id"])
    assert t["paid_until"] == inv["period_end"] and t["status"] == "trial"
    # the trial ends: paid, so the tenant becomes active (never stopped)
    clock.today = date.fromisoformat(t["trial_ends"])
    assert (await billing.daily())["activated"] == 1
    assert service.store.tenant(t["id"])["status"] == "active"
    assert "stop" not in [c[0] for c in backend.calls]
    # the portal reflects it
    page = await (await client.get("/portal")).text()
    assert "Đang hoạt động" in page and "Đã thanh toán đến" in page


async def test_portal_account_actions(client, world):
    _cfg, service, _b, backend, _mailer, _clock = world
    t = await signed_up(client, world)
    r = await post(client, "/portal/admin-password")
    assert r.status == 303
    new_pw = backend.secrets["tiem-hoa-mai"]["AI_ADMIN_PASSWORD"]
    assert (
        backend.calls[-1][0] == "reset_admin_password"
        and new_pw in await (await client.get("/portal")).text()
    )
    assert (await post(client, "/portal/cancel", cancel="1")).status == 303
    assert service.store.tenant(t["id"])["cancel_requested"] == 1
    assert (await post(client, "/portal/password", current="wrong", new="new-password-123")).status == 403
    r = await post(client, "/portal/password", current=SIGNUP["password"], new="new-password-123")
    assert (
        r.status == 303 and (await client.get("/portal", allow_redirects=False)).status == 303
    )  # logged out
    assert service.authenticate(SIGNUP["email"], "new-password-123")
    assert service.authenticate(SIGNUP["email"], SIGNUP["password"]) is None


async def test_console_actions(client, world):
    cfg, service, billing, backend, mailer, clock = world
    t = await signed_up(client, world)
    console = TestClient(TestServer(create_app(cfg, service, billing)))
    await console.start_server()
    try:
        await op_login(console)
        base = f"/console/tenants/{t['id']}"
        assert (await post(console, f"{base}/suspend")).status == 303
        assert service.store.tenant(t["id"])["status"] == "suspended" and backend.calls[-1][0] == "stop"
        assert (await post(console, f"{base}/resume")).status == 303
        assert service.store.tenant(t["id"])["status"] == "trial" and backend.calls[-1][0] == "start"
        assert (await post(console, f"{base}/plan", plan="pro")).status == 303
        assert service.store.tenant(t["id"])["plan"] == "pro"
        assert (await post(console, f"{base}/plan", plan="gold")).status == 400
        assert (await post(console, f"{base}/extend", days="7")).status == 303
        assert service.store.tenant(t["id"])["trial_ends"] == (clock.today + timedelta(days=21)).isoformat()
        # JSON API with the header: the new admin password comes back in the body
        r = await console.post(f"{base}/admin-password", json={}, headers=JSON_H)
        assert (
            r.status == 200
            and (await r.json())["admin_password"] == backend.secrets["tiem-hoa-mai"]["AI_ADMIN_PASSWORD"]
        )
        assert (await console.post(f"{base}/admin-password", json={})).status == 403  # no header
        r = await console.get("/console", headers={"Accept": "application/json"})
        assert (await r.json())["tenants"][0]["slug"] == "tiem-hoa-mai"
        # manual creation provisions right away
        r = await console.post(
            "/console/tenants",
            json={**SIGNUP, "email": "m@example.com", "slug": "manual-shop"},
            headers=JSON_H,
        )
        assert r.status == 200 and (await r.json())["admin_password"]
        await service.drain()
        assert service.store.tenant_by("slug", "manual-shop")["provision_state"] == "ready"
        # deletion needs the slug typed
        assert (await post(console, f"{base}/delete", confirm="nope")).status == 400
        assert (await post(console, f"{base}/delete", confirm="tiem-hoa-mai")).status == 303
        gone = service.store.tenant(t["id"])
        assert gone["status"] == "deleted" and backend.calls[-1] == ("destroy", "tiem-hoa-mai", True)
        assert gone["slug"] != "tiem-hoa-mai" and service.store.tenant_by("slug", "tiem-hoa-mai") is None
        assert mailer.sent[-1]["subject"] == "Đã xoá dịch vụ"
    finally:
        await console.close()
    assert (await client.get("/portal", allow_redirects=False)).status == 303  # its sessions are gone


# --------------------------------------------------------------------------- #
# The daily job


async def test_lifecycle_trial_expiry_past_due_suspend_delete(client, world):
    cfg, service, billing, backend, mailer, clock = world
    t = await signed_up(client, world)
    ends = date.fromisoformat(t["trial_ends"])
    clock.today = ends
    counts = await billing.daily()
    assert counts["suspended"] == 1 and counts["reminders"] == 2  # both reminders were due at once
    t = service.store.tenant(t["id"])
    assert t["status"] == "suspended" and ("stop", "tiem-hoa-mai", None) in backend.calls
    assert mailer.sent[-1]["subject"] == "Dịch vụ đã tạm dừng"
    # paying the trial invoice reopens the deployment
    inv = service.store.invoices(t["id"])[0]
    await billing.mark_paid(inv, "vnpay", "TXN1")
    t = service.store.tenant(t["id"])
    assert t["status"] == "active" and backend.calls[-1][0] == "start"
    assert t["paid_until"] == inv["period_end"]
    # a week before the period ends: the next invoice, once
    clock.today = date.fromisoformat(t["paid_until"]) - timedelta(days=7)
    assert (await billing.daily())["invoices"] == 1
    assert (await billing.daily())["invoices"] == 0
    assert len([i for i in service.store.invoices(t["id"]) if i["status"] == "due"]) == 1
    # unpaid past the period: past_due, then suspended after the grace days
    clock.today = date.fromisoformat(t["paid_until"]) + timedelta(days=1)
    assert (await billing.daily())["past_due"] == 1
    assert (
        service.store.tenant(t["id"])["status"] == "past_due"
        and mailer.sent[-1]["subject"] == "Hoá đơn quá hạn"
    )
    clock.today += timedelta(days=cfg.grace_days)
    assert (await billing.daily())["suspended"] == 1
    t = service.store.tenant(t["id"])
    assert t["status"] == "suspended" and t["suspended_at"] == clock.today.isoformat()
    # 30 days later: archived and deleted
    clock.today += timedelta(days=29)
    assert (await billing.daily())["deleted"] == 0
    clock.today += timedelta(days=1)
    assert (await billing.daily())["deleted"] == 1
    assert service.store.tenant(t["id"])["status"] == "deleted"
    assert backend.calls[-1] == ("destroy", "tiem-hoa-mai", True)
    assert add_month(date(2026, 1, 31)) == date(2026, 2, 28) and add_month(date(2026, 12, 5)) == date(
        2027, 1, 5
    )


async def test_cancellation_stops_invoicing(client, world):
    _cfg, service, billing, _backend, _mailer, clock = world
    t = await signed_up(client, world)
    service.request_cancellation(t, "mai@example.com")
    clock.today += timedelta(days=10)
    await billing.daily()
    assert service.store.invoices(t["id"]) == []


# --------------------------------------------------------------------------- #
# Plan limits inside the product


def test_config_limits(tmp_path):
    base = {
        "employees": [
            {"id": "a", "display_name": "A", "system_prompt": "x"},
            {"id": "b", "display_name": "B", "system_prompt": "y"},
        ]
    }
    cfg = parse_config({**base, "limits": {"users": 3, "employees": 2}}, tmp_path)
    assert cfg.limits.users == 3 and cfg.limits.employees == 2 and cfg.limits.channels is None
    assert parse_config(base, tmp_path).limits is None
    with pytest.raises(ConfigError, match="at most 1 AI employees"):
        parse_config({**base, "limits": {"employees": 1}}, tmp_path)
    with pytest.raises(ConfigError, match="unknown fields"):
        parse_config({**base, "limits": {"seats": 1}}, tmp_path)
    with pytest.raises(ConfigError, match="must be a number"):
        parse_config({**base, "limits": {"users": "ba"}}, tmp_path)


def test_users_limit(tmp_path):
    docs = DocStore(Database(str(tmp_path / "office.sqlite")))
    users = Users(docs, "owner-password-1", max_users=2)
    users.add("an", "An", "agent", "password-an-123")
    users.add("binh", "Bình", "cashier", "password-binh-1")
    with pytest.raises(ValueError, match="tối đa 2 tài khoản"):
        users.add("chi", "Chi", "agent", "password-chi-123")
    users.remove("an")
    users.add("chi", "Chi", "agent", "password-chi-123")
    # without the kwarg, a plan_limits document in the office database applies
    docs.update("plan_limits", lambda d: d.update({"users": 1}), {})
    limited = Users(docs, "owner-password-1")
    assert limited.max_users == 1
    with pytest.raises(ValueError, match="tối đa 1"):
        limited.add("dung", "Dung", "agent", "password-dung-12")
    assert Users(DocStore(Database(str(tmp_path / "o2.sqlite"))), "pw").max_users is None


# --------------------------------------------------------------------------- #
# The docker compose backend


def test_docker_backend_files_and_commands(tmp_path):
    cfg = parse_saas_config(
        {
            **RAW,
            "backend": "docker",
            "image": "registry.example.vn/aie:1.2",
            "tenants_dir": str(tmp_path / "tenants"),
            "backups_dir": str(tmp_path / "backups"),
            "caddy_dir": str(tmp_path / "caddy"),
            "caddy_reload": ["docker", "exec", "caddy", "caddy", "reload"],
            "tenant_env": {"ANTHROPIC_API_KEY": "sk-platform"},
        },
        tmp_path,
    )
    commands: list[list[str]] = []

    def record(cmd: list[str], timeout: float = 0) -> str:
        commands.append(cmd)
        return "app\n" if "ps" in cmd else ""

    backend = DockerComposeBackend(cfg, runner=record)
    tenant = {
        "id": 7,
        "slug": "hoa-mai",
        "shop_name": "Tiệm hoa Mai",
        "plan": "starter",
        "lang": "en",
        "admin_port": 20014,
        "shop_port": 20015,
    }
    backend.create(tenant, {"AI_ADMIN_PASSWORD": "pw-once", "AI_ADMIN_TOKEN": "tok"})
    d = tmp_path / "tenants" / "hoa-mai"
    conf = yaml.safe_load((d / "data" / "employees.yaml").read_text(encoding="utf-8"))
    assert conf["limits"] == {"users": 2, "employees": 1, "channels": 2, "storage_mb": 2000}
    assert (
        conf["storefront"]["public_url"] == "https://hoa-mai-shop.aie.test" and conf["staff_language"] == "en"
    )
    assert (
        conf["employees"][0]["display_name"] == "Tư vấn bán hàng"
        and conf["admin_ui"]["password_env"] == "AI_ADMIN_PASSWORD"
    )
    parse_config(conf, d / "data")  # the product accepts what we generate
    env = (d / ".env").read_text(encoding="utf-8")
    assert "AI_ADMIN_PASSWORD=pw-once" in env and "ANTHROPIC_API_KEY=sk-platform" in env
    assert (d / ".env").stat().st_mode & 0o777 == 0o600
    compose = yaml.safe_load((d / "docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["app"]["image"] == "registry.example.vn/aie:1.2"
    assert compose["services"]["app"]["ports"] == ["127.0.0.1:20014:8080", "127.0.0.1:20015:8081"]
    caddy = (tmp_path / "caddy" / "hoa-mai.caddy").read_text(encoding="utf-8")
    assert "host hoa-mai.aie.test" in caddy and "reverse_proxy 127.0.0.1:20015" in caddy
    assert commands[0][:3] == ["docker", "run", "--rm"] and "10001:10001" in commands[0]
    assert commands[1] == ["docker", "compose", "-f", str(d / "docker-compose.yml"), "up", "-d"]
    assert commands[2] == ["docker", "exec", "caddy", "caddy", "reload"]

    backend.reset_admin_password(tenant, "pw-two")
    assert (
        "AI_ADMIN_PASSWORD=pw-two" in (d / ".env").read_text()
        and "ANTHROPIC_API_KEY=sk-platform" in (d / ".env").read_text()
    )
    assert commands[-1][-3:] == ["up", "-d", "--force-recreate"]
    backend.stop(tenant)
    assert commands[-1][-1] == "stop" and backend.status(tenant) == "running"
    assert backend.usage(tenant)["disk_mb"] >= 0
    (d / "data" / "sales.db").write_bytes(b"x" * 1000)
    backup = backend.destroy(tenant, keep_backup=True)
    assert backup and Path(backup).exists() and backup.endswith(".tar.gz") and not d.exists()
    assert not (tmp_path / "caddy" / "hoa-mai.caddy").exists()
    assert next(c for c in commands if "down" in c)[-3:] == ["down", "--volumes", "--remove-orphans"]
    # no slug reaches a path or a command unless it matches the pattern
    with pytest.raises(ProvisionError):
        backend.create({**tenant, "slug": "../etc"}, {})
    with pytest.raises(ProvisionError):
        backend.stop({**tenant, "slug": "a;rm -rf /"})
    with pytest.raises(ProvisionError):
        backend.create(tenant, {"AI_ADMIN_PASSWORD": "a\nB=c"})


def test_saas_config_validation(tmp_path):
    with pytest.raises(ConfigError, match="public_url"):
        parse_saas_config({**RAW, "public_url": "saas.example"}, tmp_path)
    with pytest.raises(ConfigError, match="password_env"):
        parse_saas_config(
            {**RAW, "operators": [{"username": "ops", "password_env": "SAAS_NOPE_UNSET"}]}, tmp_path
        )
    with pytest.raises(ConfigError, match="at least one plan"):
        parse_saas_config({**RAW, "plans": {}}, tmp_path)
    cfg = parse_saas_config(RAW, tmp_path)
    assert cfg.default_plan == "starter" and cfg.admin_url("x") == "https://x.aie.test" and not cfg.https
