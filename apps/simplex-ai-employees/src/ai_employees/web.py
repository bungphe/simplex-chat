"""Admin web UI: a JSON API plus a single page, served from the office process.

Security model:
- Password login per account (users.py): the owner ("admin", password from the config)
  and staff accounts with a role; "agent" accounts reach only the inbox, optionally only
  some channels. Sessions are random tokens in HttpOnly, SameSite=Strict cookies.
- Every state-changing request must carry `X-Requested-With: ai-employees`, which a
  cross-site form or image cannot send, so a logged-in browser cannot be driven by
  another site.
- API keys are write-only: the UI can set a key, never read one back.
- Binds to 127.0.0.1 by default; put a TLS reverse proxy in front to reach it remotely.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from dataclasses import replace
from importlib import resources
from typing import TYPE_CHECKING, Any

from aiohttp import BodyPartReader, web

from . import skills as sk
from .config import EFFORT_LEVELS, AdminUIConfig, ConfigError
from .providers import PROVIDERS, ModelError
from .users import Sessions, User, Users

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
SESSIONS: web.AppKey[Sessions] = web.AppKey("sessions")
USERS: web.AppKey[Users] = web.AppKey("users")
USER: web.RequestKey[User] = web.RequestKey("user")


def _agent_may(method: str, path: str) -> bool:
    """What an "agent" (sales staff) account may call: its own account and the inbox."""
    if path in ("/api/me", "/api/me/password", "/api/logout", "/api/inbox") or path.startswith("/api/inbox/"):
        return True
    return method == "GET" and path == "/api/channels"


def _user(request: web.Request) -> User:
    return request[USER]


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
            if path != "/api/login":
                user = _session_user(request)
                if user is None:
                    raise ApiError(401, "not logged in")
                if not user.is_admin and not _agent_may(request.method, path):
                    raise ApiError(403, "Tài khoản của bạn không có quyền này")
                request[USER] = user
        resp = await handler(request)
    except ApiError as e:
        resp = _json({"error": str(e)}, status=e.status)
    except web.HTTPException:
        raise
    except Exception:
        log.exception("admin UI: %s %s failed", request.method, path)
        resp = _json({"error": "internal error"}, status=500)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)  # a handler may relax caching (attachments)
    return resp


def _session_user(request: web.Request) -> User | None:
    token = request.cookies.get(COOKIE, "")
    sessions = request.app[SESSIONS]
    session = sessions.get(token) if token else None
    if session is None:
        return None
    expiry, username = session
    user = request.app[USERS].get(username)  # a disabled or deleted account loses its sessions
    if expiry < time.time() or user is None:
        sessions.remove(token)
        return None
    return user


def _drop_sessions(app: web.Application, username: str) -> None:
    app[SESSIONS].drop_user(username)


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
    username, password = str(data.get("username", "")), str(data.get("password", ""))
    user = await asyncio.to_thread(request.app[USERS].authenticate, username, password)
    if user is None:
        await asyncio.sleep(1.0)  # slow down guessing
        log.warning("admin UI: failed login for %r from %s", username[:40], request.remote)
        raise ApiError(401, "Sai tên đăng nhập hoặc mật khẩu")
    token = secrets.token_urlsafe(32)
    request.app[SESSIONS].purge(time.time())
    request.app[SESSIONS].add(token, user.username, time.time() + SESSION_TTL)
    log.info("admin UI: %s logged in", user.username)
    resp = _json({"ok": True, "user": user.to_dict()})
    resp.set_cookie(
        COOKIE, token, httponly=True, samesite="Strict", secure=request.secure, max_age=SESSION_TTL, path="/"
    )
    return resp


async def logout(request: web.Request) -> web.Response:
    request.app[SESSIONS].remove(request.cookies.get(COOKIE, ""))
    resp = _json({"ok": True})
    resp.del_cookie(COOKIE, path="/")
    return resp


# --------------------------------------------------------------------------- #
# Accounts


async def me(request: web.Request) -> web.Response:
    return _json({"user": _user(request).to_dict()})


async def me_password(request: web.Request) -> web.Response:
    user = _user(request)
    data = await _body(request)
    if user.username == "admin":
        raise ApiError(400, "Mật khẩu của tài khoản chủ đặt trong file cấu hình (admin_ui)")
    users = request.app[USERS]
    if await asyncio.to_thread(users.authenticate, user.username, str(data.get("old", ""))) is None:
        await asyncio.sleep(1.0)
        raise ApiError(400, "Mật khẩu hiện tại không đúng")
    try:
        await asyncio.to_thread(users.update, user.username, password=str(data.get("new", "")))
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    return _json({"ok": True})


def _channel_ids(office: Office) -> set[str]:
    return {f"simplex:{e}" for e in office.employees} | set(office.hub.channels)


async def users_list(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    channels = [{"id": c, **_channel_label(office, c)} for c in sorted(_channel_ids(office))]
    return _json({"users": request.app[USERS].list(), "channels": channels})


async def users_add(request: web.Request) -> web.Response:
    data = await _body(request)
    channels = data.get("channels") or []
    if set(channels) - _channel_ids(request.app[OFFICE]):
        raise ApiError(400, "Có kênh không tồn tại")
    try:
        user = await asyncio.to_thread(
            request.app[USERS].add,
            str(data.get("username", "")),
            str(data.get("name", "")),
            str(data.get("role", "agent")),
            str(data.get("password", "")),
            channels,
        )
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    log.info("admin UI: %s added account %s (%s)", _user(request).username, user.username, user.role)
    return await users_list(request)


async def users_patch(request: web.Request) -> web.Response:
    username = request.match_info["username"]
    data = await _body(request)
    if data.get("channels") is not None and set(data["channels"]) - _channel_ids(request.app[OFFICE]):
        raise ApiError(400, "Có kênh không tồn tại")
    fields = {k: data.get(k) for k in ("name", "role", "channels", "disabled", "password")}
    if fields["password"] is not None:
        fields["password"] = str(fields["password"])
    try:
        await asyncio.to_thread(request.app[USERS].update, username, **fields)
    except KeyError:
        raise ApiError(404, "Không có tài khoản này") from None
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    if any(fields[k] is not None for k in ("role", "channels", "disabled", "password")):
        _drop_sessions(request.app, username)  # new rights or password: log in again
    return await users_list(request)


async def users_delete(request: web.Request) -> web.Response:
    username = request.match_info["username"]
    try:
        request.app[USERS].remove(username)
    except KeyError:
        raise ApiError(404, "Không có tài khoản này") from None
    _drop_sessions(request.app, username)
    return await users_list(request)


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
        "memory_pending": sum(1 for m in e.state.shared_memory if m["status"] == "pending"),
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
        "shared_memory": e.state.shared_memory,
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


async def memory_add(request: web.Request) -> web.Response:
    e = _employee(request)
    text = str((await _body(request)).get("text", "")).strip()
    if not text:
        raise ApiError(400, "Nội dung trống")
    e.state.add_memory(text, "active", f"quản trị {_user(request).name}")
    return _json(_detail(e))


async def memory_approve(request: web.Request) -> web.Response:
    e = _employee(request)
    data = await _body(request)
    fields: dict[str, Any] = {"status": "active", "approved_by": _user(request).name}
    if isinstance(data.get("text"), str) and data["text"].strip():
        fields["text"] = " ".join(data["text"].split())[:500]  # approve an edited version
    try:
        e.state.update_memory(int(request.match_info["mid"]), **fields)
    except (KeyError, ValueError):
        raise ApiError(404, "Không có ghi nhớ này") from None
    return _json(_detail(e))


async def memory_delete(request: web.Request) -> web.Response:
    e = _employee(request)
    try:
        e.state.remove_memory(int(request.match_info["mid"]))
    except (KeyError, ValueError):
        raise ApiError(404, "Không có ghi nhớ này") from None
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

    def tagged(e: Employee, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**a, "employee": e.id, "employee_name": e.settings.display_name} for a in actions]

    pending = [a for e in office.employees.values() for a in tagged(e, e.state.pending_actions())]
    pending.sort(key=lambda a: a["created"])
    recent = [
        a
        for e in office.employees.values()
        for a in tagged(e, e.state.recent_actions(150))
        if a["status"] != "pending"
    ]
    recent.sort(key=lambda a: a["created"], reverse=True)
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
    rows = [{**r, "admin": e.state.is_admin(r["id"])} for r in e.state.contact_overview()]
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
            "summary": e.state.summary(cid),
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


# --------------------------------------------------------------------------- #
# Unified inbox


def _hub(request: web.Request):
    return request.app[OFFICE].hub


def _channel_label(office: Office, channel: str) -> dict[str, str]:
    if channel.startswith("simplex:"):
        e = office.employees.get(channel.split(":", 1)[1])
        return {"type": "simplex", "name": f"SimpleX · {e.settings.display_name if e else channel}"}
    ch = office.hub.channels.get(channel)
    names = {
        "zalo_oa": "Zalo OA",
        "zalo_personal": "Zalo cá nhân",
        "facebook": "Messenger",
        "webhook": "Webhook",
        "telegram": "Telegram",
        "whatsapp": "WhatsApp",
        "email": "Email",
    }
    return {"type": ch.type if ch else "?", "name": f"{names.get(ch.type, '?') if ch else '?'} · {channel}"}


def _conv_json(office: Office, conv: Any) -> dict[str, Any]:
    from . import lang

    e = office.employees.get(conv.employee)
    language = e.state.language(conv.contact_id) if e else {}
    code = language.get("lang", "")
    return {
        **conv.to_dict(),
        "channel_info": _channel_label(office, conv.channel),
        "employee_name": e.settings.display_name if e else conv.employee,
        "lang": code,
        "lang_name": lang.name(code, "vi"),
        "lang_source": language.get("source", ""),
        "country": language.get("country", ""),
        "staff_language": office.config.staff_language,
    }


async def inbox_list(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    q = request.query
    user = _user(request)
    convs = office.hub.inbox.list(
        channel=q.get("channel") or None, mode=q.get("mode") or None, query=q.get("q") or None
    )
    channels = [
        {"id": f"simplex:{e.id}", **_channel_label(office, f"simplex:{e.id}")}
        for e in office.employees.values()
    ] + [{"id": c, **_channel_label(office, c)} for c in office.hub.channels]
    return _json(
        {
            "conversations": [_conv_json(office, c) for c in convs if user.sees(c.channel)],
            "channels": [c for c in channels if user.sees(c["id"])],
        }
    )


def _inbox_conv(request: web.Request) -> Any:
    try:
        conv = _hub(request).inbox.conversation(int(request.match_info["cid"]))
    except ValueError:
        conv = None
    if conv is None or not _user(request).sees(conv.channel):
        raise ApiError(404, "no such conversation")
    return conv


async def inbox_get(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    conv = _inbox_conv(request)
    e = office.employees.get(conv.employee)
    return _json(
        {
            "conversation": _conv_json(office, conv),
            "messages": office.hub.inbox.messages(conv.id),
            "notes": e.state.notes(conv.contact_id) if e else {},
            "summary": e.state.summary(conv.contact_id) if e else "",
            "employees": [{"id": x.id, "name": x.settings.display_name} for x in office.employees.values()],
        }
    )


async def inbox_reply(request: web.Request) -> web.Response:
    conv = _inbox_conv(request)
    data = await _body(request)
    text = str(data.get("text", "")).strip()
    if not text:
        raise ApiError(400, "Nội dung trống")
    author = _user(request).name  # who replied is the logged-in account, not a typed name
    try:
        await _hub(request).human_reply(
            conv.id,
            text,
            author,
            take_over=bool(data.get("take_over", True)),
            translate=bool(data.get("translate")),
        )
    except Exception as e:  # noqa: BLE001 - surface the platform's error to the agent
        raise ApiError(502, f"Không gửi được: {e}") from None
    return await inbox_get(request)


async def inbox_mode(request: web.Request) -> web.Response:
    conv = _inbox_conv(request)
    mode = (await _body(request)).get("mode")
    if mode not in ("ai", "human"):
        raise ApiError(400, "mode phải là ai hoặc human")
    hub = _hub(request)
    hub.inbox.set_mode(conv.id, mode)
    if mode == "ai" and hub.inbox.pending_customer_text(conv.id):
        request.app[OFFICE].cluster.request_reply(conv, 0)  # answer what is waiting
    return await inbox_get(request)


async def inbox_assign(request: web.Request) -> web.Response:
    conv = _inbox_conv(request)
    emp = str((await _body(request)).get("employee", ""))
    if emp not in request.app[OFFICE].employees or conv.is_simplex:
        raise ApiError(400, "Không đổi được nhân viên cho hội thoại này")
    _hub(request).inbox.set_employee(conv.id, emp)
    return await inbox_get(request)


async def inbox_read(request: web.Request) -> web.Response:
    conv = _inbox_conv(request)
    _hub(request).inbox.mark_read(conv.id)
    return _json({"ok": True})


async def inbox_media(request: web.Request) -> web.Response:
    """An attachment of a stored message, fetched from the platform (see media.py)."""
    from urllib.parse import quote

    from .media import INLINE_TYPES, MediaError, fetch

    conv = _inbox_conv(request)
    m = _hub(request).inbox.message(conv.id, int(request.match_info["mid"]))
    n = int(request.match_info["n"])
    if m is None or not 0 <= n < len(m["attachments"]):
        raise ApiError(404, "no such attachment")
    a = m["attachments"][n]
    url = a.get("thumb") if request.query.get("thumb") and a.get("thumb") else a.get("url") or a.get("thumb")
    channel = _hub(request).channels.get(conv.channel)
    private = url.startswith("telegram:") if url else False
    if not url or not (private or url.startswith(("https://", "http://"))):
        raise ApiError(404, "no remote file for this attachment")
    try:
        # files only the channel can download (Telegram, a WAHA server), with its credentials
        got = await channel.fetch_media(url) if channel is not None else None
        if got is None and private:
            raise MediaError("channel unavailable")
        ctype, body = got or await fetch(request.app[OFFICE].http_client, url)
    except (MediaError, Exception) as e:  # noqa: BLE001 - expired links, platform errors
        raise ApiError(502, f"Không tải được tệp: {e}") from None
    cache = {"Cache-Control": "private, max-age=3600"}
    if ctype in INLINE_TYPES:
        return web.Response(body=body, content_type=ctype, headers=cache)
    name = a.get("name") or "tep-dinh-kem"
    return web.Response(
        body=body,
        content_type="application/octet-stream",
        headers={**cache, "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


async def inbox_memory(request: web.Request) -> web.Response:
    """Staff correct what the AI remembers about this customer: the summary and saved facts."""
    conv = _inbox_conv(request)
    e = request.app[OFFICE].employees.get(conv.employee)
    if e is None:
        raise ApiError(404, "no employee for this conversation")
    data = await _body(request)
    if isinstance(data.get("summary"), str):
        e.state.set_summary(conv.contact_id, data["summary"])
    notes = data.get("notes")
    if isinstance(notes, dict):
        for key, value in notes.items():
            if value is None:
                e.state.delete_note(conv.contact_id, str(key))
            elif isinstance(value, str) and str(key).strip() and value.strip():
                try:
                    e.state.set_note(conv.contact_id, str(key), value.strip())
                except ValueError as err:
                    raise ApiError(400, str(err)) from None
    return await inbox_get(request)


async def inbox_suggest(request: web.Request) -> web.Response:
    conv = _inbox_conv(request)
    data = await _body(request) if request.can_read_body else {}
    return _json(
        {"text": await _hub(request).suggest(conv.id, in_staff_language=bool(data.get("staff_language")))}
    )


async def inbox_translate(request: web.Request) -> web.Response:
    """A customer's message in the staff language (cached on the message)."""
    from .agent import TranslationError

    conv = _inbox_conv(request)
    try:
        text = await _hub(request).translate_message(conv.id, int(request.match_info["mid"]))
    except KeyError:
        raise ApiError(404, "no such message") from None
    except TranslationError as e:
        raise ApiError(502, f"Không dịch được: {e}") from None
    return _json({"translation": text})


async def inbox_language(request: web.Request) -> web.Response:
    """Staff set the customer's country or language (kept over detection); empty = detect again."""
    from . import lang

    conv = _inbox_conv(request)
    e = request.app[OFFICE].employees.get(conv.employee)
    if e is None:
        raise ApiError(404, "no employee for this conversation")
    data = await _body(request)
    country = str(data.get("country") or "").upper()
    code = str(data.get("lang") or "")
    if country:
        if country not in lang.COUNTRIES:
            raise ApiError(400, "Không có nước này trong danh sách")
        code = code or lang.COUNTRIES[country][1]
    if code and code not in lang.LANGUAGES:
        raise ApiError(400, "Không hỗ trợ ngôn ngữ này")
    e.state.set_language(conv.contact_id, code or None, country or None)
    return await inbox_get(request)


async def languages(request: web.Request) -> web.Response:
    from . import lang

    return _json(
        {
            "languages": [{"code": c, "name": v[1], "native": v[2]} for c, v in lang.LANGUAGES.items()],
            "countries": [{"code": c, "name": v[0], "lang": v[1]} for c, v in lang.COUNTRIES.items()],
            "staff_language": request.app[OFFICE].config.staff_language,
        }
    )


async def channels_get(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    hub = office.hub
    stats = hub.inbox.stats()
    rows = []
    for e in office.employees.values():
        cid = f"simplex:{e.id}"
        rows.append(
            {
                "id": cid,
                **_channel_label(office, cid),
                "employee": e.id,
                "auto_reply": True,
                "stats": stats.get(cid, {}),
                "state": {},
            }
        )
    for c, ch in hub.channels.items():
        rows.append(
            {
                "id": c,
                **_channel_label(office, c),
                "employee": ch.cfg.employee,
                "auto_reply": ch.cfg.auto_reply,
                "poll_seconds": ch.cfg.poll_seconds,
                "stats": stats.get(c, {}),
                "state": hub.inbox.channel_state(c),
            }
        )
    user = _user(request)
    return _json({"channels": [r for r in rows if user.sees(r["id"])]})


async def channel_poll(request: web.Request) -> web.Response:
    hub = _hub(request)
    cid = request.match_info["channel"]
    if cid not in hub.channels:
        raise ApiError(404, "no such channel")
    return _json({"added": await hub.poll_once(cid), "state": hub.inbox.channel_state(cid)})


async def channel_webhook(request: web.Request) -> web.Response:
    """Telegram: point the bot at this server's /hooks/<channel id>."""
    ch = _hub(request).channels.get(request.match_info["channel"])
    if ch is None or not hasattr(ch, "register_webhook"):
        raise ApiError(404, "this channel has no webhook registration")
    if not ch.accepts_push():
        raise ApiError(400, "set the channel's secret first (Telegram sends it back on every update)")
    try:
        url = await ch.register_webhook()
    except Exception as e:  # noqa: BLE001 - wrong token, no public URL
        raise ApiError(502, str(e)) from None
    _hub(request).inbox.set_channel_state(ch.id, webhook=url)
    return _json({"ok": True, "url": url})


async def channel_login(request: web.Request) -> web.Response:
    """Zalo personal accounts: start the gateway login and return its QR (a data: URI)."""
    hub = _hub(request)
    ch = hub.channels.get(request.match_info["channel"])
    if ch is None or not hasattr(ch, "login"):
        raise ApiError(404, "this channel has no QR login")
    try:
        return _json(await ch.login())
    except Exception as e:  # noqa: BLE001 - gateway down or misconfigured
        raise ApiError(502, str(e)) from None


# --------------------------------------------------------------------------- #
# SimpleX accounts


def _qr(link: str) -> str:
    import segno

    return segno.make(link, error="m").svg_data_uri(scale=4, border=2)


async def simplex_get(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    rows = []
    for e in office.employees.values():
        address = getattr(e.bot, "address", None)
        rows.append(
            {
                "id": e.id,
                "name": e.settings.display_name,
                "address": address,
                "qr": _qr(address) if address else None,
                "contacts": len(e.state.contacts),
                "admins": len(e.state.admins),
            }
        )
    return _json({"accounts": rows})


async def simplex_invite(request: web.Request) -> web.Response:
    e = _employee(request)
    user = await e.bot.api.api_get_active_user()
    link = await e.bot.api.api_create_link(user["userId"])
    return _json({"link": link, "qr": _qr(link)})


async def simplex_connect(request: web.Request) -> web.Response:
    e = _employee(request)
    link = str((await _body(request)).get("link", "")).strip()
    if not link:
        raise ApiError(400, "Thiếu link")
    try:
        kind = await e.bot.api.api_connect_active_user(link)
    except Exception as err:  # noqa: BLE001 - invalid or already-used links
        raise ApiError(400, f"Không kết nối được: {err}") from None
    return _json({"ok": True, "kind": kind})


# --------------------------------------------------------------------------- #
# Public webhook for "webhook" channels (server-to-server, authenticated by a shared secret)


def _push_channel(request: web.Request, body: bytes = b"") -> Any:
    """The channel, if this request proves it comes from its platform or bridge."""
    ch = request.app[OFFICE].hub.channels.get(request.match_info["channel"])
    if ch is None or not ch.accepts_push():
        raise ApiError(404, "no such channel")
    if not ch.verify_push(request.headers, body, request.query):
        log.warning("hooks: rejected a request for %s from %s (bad signature)", ch.id, request.remote)
        raise ApiError(401, "bad signature")
    return ch


async def hook_verify(request: web.Request) -> web.Response:
    """Meta checks a Messenger callback URL with a GET before sending events."""
    ch = request.app[OFFICE].hub.channels.get(request.match_info["channel"])
    challenge = ch.verify_subscription(request.query) if hasattr(ch, "verify_subscription") else None
    if challenge is None:
        raise ApiError(403, "verification failed")
    return web.Response(text=challenge, content_type="text/plain")


async def _form_fields(request: web.Request, limit: int = 2 * 1024 * 1024) -> dict[str, str]:
    """The text fields of a form post. File parts (email attachments, often several MB,
    over the app's request limit) are read past and dropped: only their names are kept."""
    if not request.content_type.startswith("multipart/"):
        if (request.content_length or 0) > limit:
            raise ApiError(413, "form too large")
        return {k: v for k, v in (await request.post()).items() if isinstance(v, str)}
    fields: dict[str, str] = {}
    total = 0
    reader = await request.multipart()
    while (part := await reader.next()) is not None:
        if not isinstance(part, BodyPartReader) or part.filename or not part.name:
            if isinstance(part, BodyPartReader):
                while await part.read_chunk():
                    pass
            continue
        value = bytearray()
        while chunk := await part.read_chunk():
            total += len(chunk)
            if total > limit:
                raise ApiError(413, "form too large")
            value.extend(chunk)
        fields[part.name] = part.decode(bytes(value)).decode(part.get_charset(default="utf-8"), "replace")
    return fields


async def hook_inbound(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    target = office.hub.channels.get(request.match_info["channel"])
    form = target is not None and target.push_format == "form"
    # form posts (inbound email) are authenticated by the URL key or basic auth, not the body
    body = b"" if form else await request.read()
    ch = _push_channel(request, body)
    # The Zalo gateway posts to {WEBHOOK_URL}/{account}: the account must be this channel's.
    if (account := request.match_info.get("account")) is not None and account != getattr(ch, "account", None):
        raise ApiError(404, "no such account")
    try:
        if form:
            payload = await _form_fields(request)
        else:
            payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ApiError(400, "expected a JSON object")
        conv = office.hub.push_inbound(ch.id, payload)
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    return _json({"ok": True, "conversation": conv.id if conv else None})


async def hook_messages(request: web.Request) -> web.Response:
    """Replies for a bridge that polls instead of receiving reply_url calls."""
    office = request.app[OFFICE]
    ch = _push_channel(request)
    if ch.type != "webhook":
        raise ApiError(404, "no such channel")
    conv = office.hub.inbox.find(ch.id, request.match_info["conversation"])
    try:
        after = int(request.query.get("after", "0"))
    except ValueError:
        raise ApiError(400, "after must be a message id") from None
    msgs = [m for m in office.hub.inbox.messages(conv.id) if m["id"] > after] if conv else []
    return _json({"messages": msgs})


def create_app(office: Office, password: str) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=256 * 1024)
    app[OFFICE] = office
    app[PASSWORD] = password
    app[SESSIONS] = Sessions(office.office_db)
    app[USERS] = Users(office.docs, password, legacy_path=os.path.join(office.config.state_dir, "users.json"))
    r = app.router
    r.add_get("/", page)
    r.add_get("/static/{file}", page)
    r.add_get("/favicon.ico", no_content)
    r.add_post("/api/login", login)
    r.add_post("/api/logout", logout)
    r.add_get("/api/me", me)
    r.add_post("/api/me/password", me_password)
    r.add_get("/api/users", users_list)
    r.add_post("/api/users", users_add)
    r.add_patch("/api/users/{username}", users_patch)
    r.add_delete("/api/users/{username}", users_delete)
    r.add_get("/api/overview", overview)
    r.add_get("/api/employees/{emp}", employee_get)
    r.add_patch("/api/employees/{emp}", employee_patch)
    r.add_post("/api/employees/{emp}/reset", employee_reset)
    r.add_post("/api/employees/{emp}/corrections", correction_add)
    r.add_post("/api/employees/{emp}/memory", memory_add)
    r.add_post("/api/employees/{emp}/memory/{mid}/approve", memory_approve)
    r.add_delete("/api/employees/{emp}/memory/{mid}", memory_delete)
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
    r.add_get("/api/inbox", inbox_list)
    r.add_get("/api/inbox/languages", languages)  # before {cid}
    r.add_get("/api/inbox/{cid}", inbox_get)
    r.add_post("/api/inbox/{cid}/reply", inbox_reply)
    r.add_post("/api/inbox/{cid}/mode", inbox_mode)
    r.add_post("/api/inbox/{cid}/assign", inbox_assign)
    r.add_post("/api/inbox/{cid}/read", inbox_read)
    r.add_post("/api/inbox/{cid}/suggest", inbox_suggest)
    r.add_post("/api/inbox/{cid}/memory", inbox_memory)
    r.add_post(r"/api/inbox/{cid:\d+}/messages/{mid:\d+}/translate", inbox_translate)
    r.add_post("/api/inbox/{cid}/language", inbox_language)
    r.add_get(r"/api/inbox/{cid:\d+}/media/{mid:\d+}/{n:\d+}", inbox_media)
    r.add_get("/api/channels", channels_get)
    r.add_post("/api/channels/{channel}/poll", channel_poll)
    r.add_get("/api/simplex", simplex_get)
    r.add_post("/api/simplex/{emp}/invite", simplex_invite)
    r.add_post("/api/simplex/{emp}/connect", simplex_connect)
    r.add_post("/api/channels/{channel}/login", channel_login)
    r.add_post("/api/channels/{channel}/webhook", channel_webhook)
    r.add_get("/hooks/{channel}", hook_verify)
    r.add_post("/hooks/{channel}", hook_inbound)
    r.add_post("/hooks/{channel}/{account}", hook_inbound)
    r.add_get("/hooks/{channel}/{conversation}", hook_messages)
    return app


async def start_admin_ui(office: Office, ui: AdminUIConfig) -> web.AppRunner:
    if not ui.password:
        raise ConfigError("admin_ui needs a password (password_env or password)")
    if len(ui.password) < 12:
        log.warning("admin UI: the password is short; use at least 12 characters")
    if os.environ.get("AI_ADMIN_UI_HOST"):
        log.info("admin UI host set by AI_ADMIN_UI_HOST (container); publish the port on 127.0.0.1 only")
    elif ui.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("admin UI listens on %s: put it behind a TLS reverse proxy", ui.host)
    runner = web.AppRunner(create_app(office, ui.password), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, ui.host, ui.port).start()
    log.info("admin UI: http://%s:%s", ui.host, ui.port)
    return runner
