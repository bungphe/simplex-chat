"""Web API of the projects and tasks page (projects.py): every staff member sees the
projects and works on tasks; store managers and admins create, rename and delete
projects, and delete any task (others only the tasks they created). Registered by
web.create_app; the page's area is "projects" (web._may)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from aiohttp import web

from .i18n import tr
from .projects import MAX_FILE, ProjectError, Projects


def _w() -> Any:
    from . import web as w

    return w


def _pm(request: web.Request) -> Projects:
    return request.app[_w().OFFICE].projects


def _actor(request: web.Request) -> str:
    return _w()._user(request).username


def _manager(request: web.Request) -> bool:
    return Projects.manages(_w()._user(request))


def _num(request: web.Request, key: str = "id") -> int:
    value = request.match_info[key]
    if len(value) > 15:  # no such row (and too big for the database)
        raise _w().ApiError(404, tr("không tìm thấy {0}", value))
    return _w()._int(value, key)


def handler(fn: Any) -> Any:
    """fn(request, data) -> JSON; ProjectError -> 400, KeyError -> 404, PermissionError -> 403."""

    async def run(request: web.Request) -> web.StreamResponse:
        w = _w()
        data = (
            await w._body(request)
            if request.method in ("POST", "PUT", "PATCH") and request.can_read_body
            else {}
        )
        try:
            result = fn(request, data)
            if hasattr(result, "__await__"):
                result = await result
        except (ProjectError, ValueError, TypeError) as e:
            raise w.ApiError(400, str(e)) from None
        except PermissionError as e:
            raise w.ApiError(403, str(e)) from None
        except KeyError as e:
            raise w.ApiError(404, tr("không tìm thấy {0}", str(e).strip("'\""))) from None
        return result if isinstance(result, web.StreamResponse) else w._json(result)

    return run


def _only_managers(request: web.Request) -> None:
    if not _manager(request):
        raise PermissionError(tr("Chỉ quản lý cửa hàng hoặc quản trị viên làm được việc này"))


# ---------------------------------------------------------------------- #
# projects


def projects_list(request: web.Request, _d: dict[str, Any]) -> Any:
    pm = _pm(request)
    archived = request.query.get("archived") == "1"
    return {"projects": pm.projects(archived=archived), "people": pm.people(), "manager": _manager(request)}


def project_create(request: web.Request, data: dict[str, Any]) -> Any:
    _only_managers(request)
    return _pm(request).save_project(None, data, _actor(request))


def project_patch(request: web.Request, data: dict[str, Any]) -> Any:
    _only_managers(request)
    return _pm(request).save_project(_num(request), data, _actor(request))


def project_delete(request: web.Request, _d: dict[str, Any]) -> Any:
    _only_managers(request)
    _pm(request).delete_project(_num(request), _actor(request))
    return {"ok": True}


def project_board(request: web.Request, _d: dict[str, Any]) -> Any:
    pm = _pm(request)
    return {**pm.board(_num(request)), "people": pm.people(), "manager": _manager(request)}


def project_log(request: web.Request, _d: dict[str, Any]) -> Any:
    limit = _w()._int(request.query.get("limit", "200"), "limit")
    return {"log": _pm(request).log(_num(request), limit)}


def project_export(request: web.Request, _d: dict[str, Any]) -> Any:
    pm = _pm(request)
    data = pm.export(_num(request))
    name = f"{data['project']['name']}.json"
    return web.Response(
        text=json.dumps(data, ensure_ascii=False, indent=1),
        content_type="application/json",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


def project_csv(request: web.Request, _d: dict[str, Any]) -> Any:
    pm = _pm(request)
    pid = _num(request)
    name = f"{pm.project(pid)['name']}.csv"
    return web.Response(
        text=pm.csv(pid),
        content_type="text/csv",
        charset="utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


MAX_IMPORT = 64 * 1024 * 1024  # an export with long notes on thousands of tasks


async def project_import(request: web.Request) -> web.StreamResponse:
    """The export file is the request body (read here: it is far bigger than a form)."""
    w = _w()
    try:
        _only_managers(request)
    except PermissionError as e:
        raise w.ApiError(403, str(e)) from None
    chunks, size = [], 0
    while chunk := await request.content.read(65536):
        size += len(chunk)
        if size > MAX_IMPORT:
            raise w.ApiError(413, tr("Tệp quá lớn (tối đa {0} MB)", MAX_IMPORT // 1024 // 1024))
        chunks.append(chunk)
    try:
        data = json.loads(b"".join(chunks))
    except ValueError:
        raise w.ApiError(400, tr("Không phải tệp dự án")) from None
    try:
        return w._json(_pm(request).import_project(data, _actor(request)))
    except (ProjectError, ValueError, TypeError) as e:
        raise w.ApiError(400, str(e)) from None


# ---------------------------------------------------------------------- #
# tasks


def _parent(value: Any) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    return _w()._int(value, "parent_id")


def task_create(request: web.Request, data: dict[str, Any]) -> Any:
    after = data.get("after_id")
    return _pm(request).create_task(
        _num(request),
        data,
        _actor(request),
        parent_id=_parent(data.get("parent_id")),
        after_id=_w()._int(after, "after_id") if after not in (None, "") else None,
    )


def task_get(request: web.Request, _d: dict[str, Any]) -> Any:
    return _pm(request).task(_num(request))


def task_patch(request: web.Request, data: dict[str, Any]) -> Any:
    if "parent_id" in data:
        data = {**data, "parent_id": _parent(data["parent_id"])}
    return _pm(request).update_task(_num(request), data, _actor(request))


def task_move(request: web.Request, data: dict[str, Any]) -> Any:
    pm = _pm(request)
    tid = _num(request)
    parent = _parent(data["parent_id"]) if "parent_id" in data else "keep"
    return pm.move(tid, _actor(request), parent_id=parent, direction=str(data.get("direction", "")))


def task_delete(request: web.Request, _d: dict[str, Any]) -> Any:
    pm = _pm(request)
    tid = _num(request)
    if not pm.may_delete_task(_w()._user(request), tid):
        raise PermissionError(tr("Chỉ người tạo hoặc quản lý xoá được công việc"))
    return {"deleted": pm.delete_task(tid, _actor(request))}


def my_tasks(request: web.Request, _d: dict[str, Any]) -> Any:
    return {"tasks": _pm(request).my_tasks(_actor(request))}


def comments_list(request: web.Request, _d: dict[str, Any]) -> Any:
    return {"comments": _pm(request).comments(_num(request))}


def comment_add(request: web.Request, data: dict[str, Any]) -> Any:
    return _pm(request).add_comment(_num(request), str(data.get("text", "")), _actor(request))


def comment_delete(request: web.Request, _d: dict[str, Any]) -> Any:
    _pm(request).delete_comment(_num(request), _actor(request), _manager(request))
    return {"ok": True}


def files_list(request: web.Request, _d: dict[str, Any]) -> Any:
    return {"files": _pm(request).files(_num(request)), "max_size": MAX_FILE}


async def file_upload(request: web.Request) -> web.StreamResponse:
    """The file is the request body (read here, not by aiohttp, whose limit is for forms);
    its name is in ?name=."""
    w = _w()
    pm = _pm(request)
    tid = _num(request)
    chunks, size = [], 0
    while chunk := await request.content.read(65536):
        size += len(chunk)
        if size > MAX_FILE:
            raise w.ApiError(413, tr("Tệp quá lớn (tối đa {0} MB)", MAX_FILE // 1024 // 1024))
        chunks.append(chunk)
    try:
        row = pm.add_file(
            tid, request.query.get("name", "file"), request.content_type, b"".join(chunks), _actor(request)
        )
    except ProjectError as e:
        raise w.ApiError(400, str(e)) from None
    except KeyError as e:
        raise w.ApiError(404, tr("không tìm thấy {0}", str(e).strip("'\""))) from None
    return w._json(row)


async def file_download(request: web.Request) -> web.StreamResponse:
    w = _w()
    try:
        f = _pm(request).file(_num(request))
    except KeyError as e:
        raise w.ApiError(404, tr("không tìm thấy {0}", str(e).strip("'\""))) from None
    return web.Response(
        body=f["data"],
        headers={
            # never shown inline: a file from a colleague is not a page of this site
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(f['name'])}",
            "X-Content-Type-Options": "nosniff",
        },
    )


def file_delete(request: web.Request, _d: dict[str, Any]) -> Any:
    _pm(request).delete_file(_num(request), _actor(request), _manager(request))
    return {"ok": True}


def add_routes(r: web.UrlDispatcher) -> None:
    r.add_get("/api/pm/projects", handler(projects_list))
    r.add_post("/api/pm/projects", handler(project_create))
    r.add_post("/api/pm/import", project_import)
    r.add_get("/api/pm/my", handler(my_tasks))
    r.add_get(r"/api/pm/projects/{id:\d+}", handler(project_board))
    r.add_patch(r"/api/pm/projects/{id:\d+}", handler(project_patch))
    r.add_delete(r"/api/pm/projects/{id:\d+}", handler(project_delete))
    r.add_get(r"/api/pm/projects/{id:\d+}/log", handler(project_log))
    r.add_get(r"/api/pm/projects/{id:\d+}/export.json", handler(project_export))
    r.add_get(r"/api/pm/projects/{id:\d+}/tasks.csv", handler(project_csv))
    r.add_post(r"/api/pm/projects/{id:\d+}/tasks", handler(task_create))
    r.add_get(r"/api/pm/tasks/{id:\d+}", handler(task_get))
    r.add_patch(r"/api/pm/tasks/{id:\d+}", handler(task_patch))
    r.add_post(r"/api/pm/tasks/{id:\d+}/move", handler(task_move))
    r.add_delete(r"/api/pm/tasks/{id:\d+}", handler(task_delete))
    r.add_get(r"/api/pm/tasks/{id:\d+}/comments", handler(comments_list))
    r.add_post(r"/api/pm/tasks/{id:\d+}/comments", handler(comment_add))
    r.add_delete(r"/api/pm/comments/{id:\d+}", handler(comment_delete))
    r.add_get(r"/api/pm/tasks/{id:\d+}/files", handler(files_list))
    r.add_put(r"/api/pm/tasks/{id:\d+}/files", file_upload)
    r.add_get(r"/api/pm/files/{id:\d+}", file_download)
    r.add_delete(r"/api/pm/files/{id:\d+}", handler(file_delete))
