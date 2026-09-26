"""Admin web UI: a JSON API plus a single page, served from the office process.

Security model:
- Password login; sessions are random tokens in HttpOnly, SameSite=Strict cookies.
- Every state-changing request must carry `X-Requested-With: ai-employees`, which a
  cross-site form or image cannot send, so a logged-in browser cannot be driven by
  another site.
- API keys are write-only: the UI can set a key, never read one back.
- Binds to 127.0.0.1 by default; put a TLS reverse proxy in front to reach it remotely.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import time
from dataclasses import replace
from importlib import resources
from typing import TYPE_CHECKING, Any

from aiohttp import web

from . import skills as sk
from .config import EFFORT_LEVELS, AdminUIConfig, ConfigError
from .providers import PROVIDERS, ModelError

if TYPE_CHECKING:
    from .employee import Employee, Office

log = logging.getLogger(__name__)

COOKIE = "aie_session"
SESSION_TTL = 12 * 3600
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "ai-employees"
STATIC = ("admin.html", "admin.js", "admin.css")
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

OFFICE: web.AppKey[Office] = web.AppKey("office")
PASSWORD: web.AppKey[str] = web.AppKey("password")
SESSIONS: web.AppKey[dict[str, float]] = web.AppKey("sessions")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, ensure_ascii=False))


@web.middleware
async def guard(request: web.Request, handler: Any) -> web.StreamResponse:
    path = request.path
    try:
        if path.startswith("/api/"):
            if request.method not in ("GET", "HEAD") and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
                raise ApiError(403, "missing request header")
            if path != "/api/login" and not _session_valid(request):
                raise ApiError(401, "not logged in")
        resp = await handler(request)
    except ApiError as e:
        resp = _json({"error": str(e)}, status=e.status)
    except web.HTTPException:
        raise
    except Exception:
        log.exception("admin UI: %s %s failed", request.method, path)
        resp = _json({"error": "internal error"}, status=500)
    resp.headers.update(SECURITY_HEADERS)
    return resp


def _session_valid(request: web.Request) -> bool:
    token = request.cookies.get(COOKIE, "")
    sessions = request.app[SESSIONS]
    expiry = sessions.get(token)
    if expiry is None:
        return False
    if expiry < time.time():
        sessions.pop(token, None)
        return False
    return True


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except json.JSONDecodeError:
        raise ApiError(400, "invalid JSON") from None
    if not isinstance(data, dict):
        raise ApiError(400, "expected a JSON object")
    return data


def _employee(request: web.Request) -> Employee:
    e = request.app[OFFICE].employees.get(request.match_info["emp"])
    if e is None:
        raise ApiError(404, "no such employee")
    return e


# --------------------------------------------------------------------------- #
# Pages and login


async def page(request: web.Request) -> web.Response:
    name = request.match_info.get("file") or "admin.html"
    if name not in STATIC:
        raise web.HTTPNotFound()
    body = resources.files("ai_employees").joinpath("static", name).read_text(encoding="utf-8")
    ctype = {"html": "text/html", "js": "application/javascript", "css": "text/css"}[name.rsplit(".", 1)[1]]
    return web.Response(text=body, content_type=ctype, charset="utf-8")


async def no_content(request: web.Request) -> web.Response:
    return web.Response(status=204)


async def login(request: web.Request) -> web.Response:
    data = await _body(request)
    password = str(data.get("password", ""))
    if not hmac.compare_digest(password.encode(), request.app[PASSWORD].encode()):
        await asyncio.sleep(1.0)  # slow down guessing
        log.warning("admin UI: failed login from %s", request.remote)
        raise ApiError(401, "Sai mật khẩu")
    token = secrets.token_urlsafe(32)
    request.app[SESSIONS][token] = time.time() + SESSION_TTL
    resp = _json({"ok": True})
    resp.set_cookie(
        COOKIE, token, httponly=True, samesite="Strict", secure=request.secure, max_age=SESSION_TTL, path="/"
    )
    return resp


async def logout(request: web.Request) -> web.Response:
    request.app[SESSIONS].pop(request.cookies.get(COOKIE, ""), None)
    resp = _json({"ok": True})
    resp.del_cookie(COOKIE, path="/")
    return resp


# --------------------------------------------------------------------------- #
# Overview and employees


def _routines(e: Employee) -> list[dict[str, Any]]:
    s = e.settings
    local = e.local_now()
    out = []
    for r in s.routines:
        st = e.state.routine(r.id)
        nxt = r.next_start(local, st.get("period"))
        out.append(
            {
                "id": r.id,
                "schedule": r.describe(),
                "task": r.task,
                "deliver": r.deliver,
                "paused": r.id in s.paused_routines,
                "next": nxt.isoformat(timespec="minutes") if nxt else None,
                "next_local": f"{nxt:%H:%M %d/%m}" if nxt else None,
                "timezone": s.timezone,
                "last_run": st.get("last_run"),
                "last_status": st.get("last_status"),
                "last_output": st.get("last_output"),
            }
        )
    return out


def _summary(e: Employee, stats: dict[str, Any]) -> dict[str, Any]:
    s = e.settings
    profile = e.office.model_profile(s.model)
    return {
        "id": e.id,
        "display_name": s.display_name,
        "short_descr": s.short_descr,
        "paused": s.paused,
        "model": s.model,
        "model_desc": profile.describe() if profile else "?",
        "address": getattr(e.bot, "address", None),
        "admins": len(e.state.admins),
        "contacts": len(e.state.contacts),
        "pending": len(e.actions.pending()),
        "routines": _routines(e),
        "stats": stats.get(e.id, {}),
    }


async def overview(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    stats = office.runlog.summary(hours=24)
    return _json({"employees": [_summary(e, stats) for e in office.employees.values()]})


def _detail(e: Employee) -> dict[str, Any]:
    s = e.settings
    office = e.office
    return {
        **_summary(e, office.runlog.summary(hours=24)),
        "system_prompt": s.system_prompt,
        "effort": s.effort,
        "effort_levels": list(EFFORT_LEVELS),
        "skills": list(s.skills),
        "available_skills": [
            {
                "name": n,
                "description": sk.REGISTRY[n].description
                if n in sk.REGISTRY
                else "Nhóm skill: " + ", ".join(sk.GROUPS.get(n, ())),
            }
            for n in sk.available()
        ],
        "releases": list(s.releases),
        "actions": list(office.config.actions),
        "corrections": list(s.corrections),
        "models": office.model_names(),
        "overrides": list(e.state.overrides),
        "admin_contacts": [{"id": c, "name": e.state.contact_name(c)} for c in e.state.admins],
        "welcome": s.welcome,
    }


async def employee_get(request: web.Request) -> web.Response:
    return _json(_detail(_employee(request)))


async def employee_patch(request: web.Request) -> web.Response:
    e = _employee(request)
    data = await _body(request)
    office = request.app[OFFICE]
    st = e.state
    if "system_prompt" in data:
        text = str(data["system_prompt"]).strip()
        if not text:
            raise ApiError(400, "Prompt không được để trống")
        st.set_override("system_prompt", text)
    if "model" in data:
        if office.model_profile(str(data["model"])) is None:
            raise ApiError(400, f"Model '{data['model']}' chưa được khai báo")
        st.set_override("model", str(data["model"]))
    if "effort" in data:
        if data["effort"] is not None and data["effort"] not in EFFORT_LEVELS:
            raise ApiError(400, "effort không hợp lệ")
        st.set_override("effort", data["effort"])
    if "paused" in data:
        st.set_override("paused", bool(data["paused"]))
    if "skills" in data:
        skills = [str(x) for x in data["skills"]]
        if unknown := [x for x in skills if x not in sk.available()]:
            raise ApiError(400, f"Skill không tồn tại: {', '.join(unknown)}")
        st.set_override("skills", skills)
    if "releases" in data:
        releases = [str(x) for x in data["releases"]]
        if unknown := [x for x in releases if x not in office.config.actions]:
            raise ApiError(400, f"Hành động không tồn tại: {', '.join(unknown)}")
        st.set_override("releases", releases)
    return _json(_detail(e))


async def employee_reset(request: web.Request) -> web.Response:
    e = _employee(request)
    e.state.clear_overrides()
    return _json(_detail(e))


async def correction_add(request: web.Request) -> web.Response:
    e = _employee(request)
    text = str((await _body(request)).get("text", "")).strip()
    if not text:
        raise ApiError(400, "Nội dung trống")
    e.add_correction(text)
    return _json(_detail(e))


async def correction_delete(request: web.Request) -> web.Response:
    e = _employee(request)
    try:
        e.remove_correction(int(request.match_info["n"]))
    except (ValueError, IndexError):
        raise ApiError(404, "no such correction") from None
    return _json(_detail(e))


async def admin_remove(request: web.Request) -> web.Response:
    e = _employee(request)
    e.state.remove_admin(int(request.match_info["cid"]))
    return _json(_detail(e))


# --------------------------------------------------------------------------- #
# Routines


async def routine_run(request: web.Request) -> web.Response:
    e = _employee(request)
    r = e.settings.routine(request.match_info["rid"])
    if r is None:
        raise ApiError(404, "no such routine")
    res = await e.run_routine(r, e.local_now(), manual=True)
    return _json({"status": res.status, "text": res.text})


async def routine_pause(request: web.Request) -> web.Response:
    e = _employee(request)
    rid = request.match_info["rid"]
    if e.settings.routine(rid) is None:
        raise ApiError(404, "no such routine")
    e.set_routine_paused(rid, bool((await _body(request)).get("paused")))
    return _json(_detail(e))


# --------------------------------------------------------------------------- #
# Approvals


async def approvals(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    items = [
        {**a, "employee": e.id, "employee_name": e.settings.display_name}
        for e in office.employees.values()
        for a in e.state.actions
    ]
    items.sort(key=lambda a: (a["status"] != "pending", a["created"]), reverse=False)
    pending = [a for a in items if a["status"] == "pending"]
    recent = sorted((a for a in items if a["status"] != "pending"), key=lambda a: a["created"], reverse=True)
    return _json({"pending": pending, "recent": recent[:100]})


async def approval_decide(request: web.Request) -> web.Response:
    e = _employee(request)
    n = int(request.match_info["n"])
    decision = request.match_info["decision"]
    by = "web admin"
    if decision == "approve":
        msg = await e.actions.approve(n, by=by)
    elif decision == "reject":
        msg = await e.actions.reject(n, by=by, reason=str((await _body(request)).get("reason", "")).strip())
    else:
        raise ApiError(404, "unknown decision")
    return _json({"message": msg, "action": e.state.action(n)})


# --------------------------------------------------------------------------- #
# Models


def _model_rows(office: Office) -> list[dict[str, Any]]:
    rows = []
    for name in office.model_names():
        p = office.model_profile(name)
        if p is None:
            continue
        rows.append(
            {
                "name": name,
                "provider": p.provider,
                "model": p.model,
                "base_url": p.base_url,
                "api_key_env": p.api_key_env,
                "has_key": bool(p.key()),
                "source": "ui" if name in office.runtime_models else "config",
                "used_by": [e.id for e in office.employees.values() if e.settings.model == name],
                "describe": p.describe(),
            }
        )
    return rows


async def models_get(request: web.Request) -> web.Response:
    return _json({"models": _model_rows(request.app[OFFICE]), "providers": list(PROVIDERS)})


async def model_add(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    data = await _body(request)
    name = str(data.pop("name", "")).strip()
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        raise ApiError(400, "Tên model chỉ gồm chữ, số, '-' hoặc '_'")
    raw = {k: v for k, v in data.items() if v not in (None, "", {})}
    if isinstance(raw.get("extra_body"), str):
        try:
            raw["extra_body"] = json.loads(raw["extra_body"])
        except json.JSONDecodeError:
            raise ApiError(400, "extra_body phải là JSON") from None
    try:
        office.add_runtime_model(name, raw)
    except ConfigError as e:
        raise ApiError(400, str(e)) from None
    return _json({"models": _model_rows(office)})


async def model_delete(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    name = request.match_info["name"]
    try:
        office.remove_runtime_model(name)
    except KeyError:
        raise ApiError(
            400, "Chỉ xoá được model thêm từ giao diện; model trong file cấu hình sửa trong file"
        ) from None
    return _json({"models": _model_rows(office)})


async def model_test(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    name = request.match_info["name"]
    model = office.model_for(name)
    if model is None:
        raise ApiError(404, "no such model")
    base = next(iter(office.employees.values())).base
    settings = replace(base, effort=None, max_tokens=512)
    started = time.monotonic()
    try:
        turn = await model.step(
            system=("Reply with the single word OK.", "This is a connection test."),
            messages=model.messages([], "ping"),
            tools=[],
            settings=settings,
        )
    except ModelError as e:
        return _json({"ok": False, "error": str(e)[:500]})
    ms = int((time.monotonic() - started) * 1000)
    return _json({"ok": True, "text": turn.text[:200], "ms": ms, "stop": turn.stop})


# --------------------------------------------------------------------------- #
# Conversations and run log


async def conversations(request: web.Request) -> web.Response:
    e = _employee(request)
    rows = []
    for cid, name in e.state.contacts.items():
        turns = e.state.timed_history(cid)
        rows.append(
            {
                "id": cid,
                "name": name,
                "turns": len(turns) // 2,
                "last": turns[-1].get("ts") if turns else None,
                "admin": e.state.is_admin(cid),
            }
        )
    rows.sort(key=lambda r: r["last"] or "", reverse=True)
    return _json({"contacts": rows})


async def conversation_get(request: web.Request) -> web.Response:
    e = _employee(request)
    cid = int(request.match_info["cid"])
    return _json(
        {
            "id": cid,
            "name": e.state.contact_name(cid),
            "turns": e.state.timed_history(cid),
            "notes": e.state.notes(cid),
        }
    )


async def conversation_forget(request: web.Request) -> web.Response:
    e = _employee(request)
    e.state.forget(int(request.match_info["cid"]))
    return _json({"ok": True})


async def runlog(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    q = request.query
    limit = max(1, min(int(q.get("limit", "200")), 2000))
    rows = office.runlog.tail(limit=limit, employee=q.get("employee") or None, kind=q.get("kind") or None)
    return _json({"records": list(reversed(rows))})


# --------------------------------------------------------------------------- #


def create_app(office: Office, password: str) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=256 * 1024)
    app[OFFICE] = office
    app[PASSWORD] = password
    app[SESSIONS] = {}
    r = app.router
    r.add_get("/", page)
    r.add_get("/static/{file}", page)
    r.add_get("/favicon.ico", no_content)
    r.add_post("/api/login", login)
    r.add_post("/api/logout", logout)
    r.add_get("/api/overview", overview)
    r.add_get("/api/employees/{emp}", employee_get)
    r.add_patch("/api/employees/{emp}", employee_patch)
    r.add_post("/api/employees/{emp}/reset", employee_reset)
    r.add_post("/api/employees/{emp}/corrections", correction_add)
    r.add_delete("/api/employees/{emp}/corrections/{n}", correction_delete)
    r.add_delete("/api/employees/{emp}/admins/{cid}", admin_remove)
    r.add_post("/api/employees/{emp}/routines/{rid}/run", routine_run)
    r.add_post("/api/employees/{emp}/routines/{rid}/pause", routine_pause)
    r.add_get("/api/employees/{emp}/conversations", conversations)
    r.add_get("/api/employees/{emp}/conversations/{cid}", conversation_get)
    r.add_delete("/api/employees/{emp}/conversations/{cid}", conversation_forget)
    r.add_get("/api/approvals", approvals)
    r.add_post("/api/approvals/{emp}/{n}/{decision}", approval_decide)
    r.add_get("/api/models", models_get)
    r.add_post("/api/models", model_add)
    r.add_delete("/api/models/{name}", model_delete)
    r.add_post("/api/models/{name}/test", model_test)
    r.add_get("/api/runlog", runlog)
    return app


async def start_admin_ui(office: Office, ui: AdminUIConfig) -> web.AppRunner:
    if not ui.password:
        raise ConfigError("admin_ui needs a password (password_env or password)")
    if len(ui.password) < 12:
        log.warning("admin UI: the password is short; use at least 12 characters")
    if ui.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("admin UI listens on %s: put it behind a TLS reverse proxy", ui.host)
    runner = web.AppRunner(create_app(office, ui.password), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, ui.host, ui.port).start()
    log.info("admin UI: http://%s:%s", ui.host, ui.port)
    return runner
