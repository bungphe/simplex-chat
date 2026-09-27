"""An AI employee = one SimpleX account + an agent + chat-based admin commands.
The office runs every employee, the routine scheduler and the admin web UI."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections import defaultdict
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx2
from simplex_chat import Bot, BotCommand, BotProfile, Message, SqliteDb

from . import skills as sk
from .actions import ActionDesk
from .agent import Agent, RunResult
from .config import EFFORT_LEVELS, AppConfig, ConfigError, EmployeeConfig, parse_models
from .db import Database, DocStore, connect
from .llm import LLM
from .providers import ChatModel, ModelProfile, make_model
from .routines import Routine
from .runlog import RunLog
from .state import EmployeeState, now_iso

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 4000

ADMIN_HELP = """\
*Lệnh quản trị nhân viên AI*
/ai show — xem cấu hình hiện tại
/ai prompt <nội dung> — đặt vai trò / hướng dẫn (system prompt)
/ai correct <quy tắc> — thêm quy tắc sửa sai (ưu tiên hơn prompt); /ai corrections; /ai uncorrect <số>
/ai models — xem các model AI; /ai model <tên> — gán model
/ai effort <low|medium|high|xhigh|max|off> — mức suy nghĩ
/ai skills — xem skill; /ai skill add|remove <tên>
/ai routines — lịch làm việc; /ai run <id> — chạy ngay; /ai routine pause|resume <id>
/ai pending — yêu cầu chờ duyệt; /ai approve <số>; /ai reject <số> [lý do]
/ai releases — hành động được tự làm; /ai release <hành động>; /ai hold <hành động>
/ai pause — tạm dừng mọi việc; /ai resume — bật lại
/ai forget all — xoá toàn bộ trí nhớ hội thoại
/ai reset — bỏ mọi thay đổi, quay về file cấu hình"""


class EmployeeBot(Bot):
    """Bot that can be pinned to specific SMP servers and exposes its address."""

    def __init__(self, *, smp_servers: tuple[str, ...] = (), **kw: Any):
        super().__init__(**kw)
        self.smp_servers = smp_servers
        self.address: str | None = None

    async def _post_start(self, user: Any) -> None:
        # Runs after start_chat (required by /smp) and before the address is created.
        if self.smp_servers:
            await self.api.send_chat_cmd("/smp " + " ".join(self.smp_servers))
        await super()._post_start(user)

    async def _sync_address(self, user: Any) -> str | None:
        self.address = await super()._sync_address(user)
        return self.address


class Employee:
    def __init__(self, cfg: EmployeeConfig, office: Office, state_dir: str):
        self.id = cfg.id
        self.base = cfg
        self.office = office
        self.state = EmployeeState(Path(state_dir) / f"{cfg.id}.json", db=office.db, employee=cfg.id)
        self.agent = Agent(self)
        self.actions = ActionDesk(self)
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: set[asyncio.Task[Any]] = set()
        Path(cfg.db).parent.mkdir(parents=True, exist_ok=True)
        self.bot = EmployeeBot(
            smp_servers=office.config.smp_servers,
            profile=BotProfile(display_name=cfg.display_name, short_descr=cfg.short_descr),
            db=SqliteDb(file_prefix=cfg.db),
            welcome=cfg.welcome,
            commands=[BotCommand(keyword="forget", label="Xoá lịch sử trò chuyện / Forget me")],
        )
        self.bot.on_message(content_type="text", chat_type="direct")(self._on_text)
        self.bot.on_message(chat_type="direct")(self._on_other)

    @property
    def settings(self) -> EmployeeConfig:
        """Config file values with the admin's runtime overrides applied."""
        return self.base.with_overrides(self.state.overrides)

    def chat_model(self) -> ChatModel:
        """The model assigned to this employee (an admin may have reassigned it)."""
        model = self.office.model_for(self.settings.model)
        if model is None:  # override names a model no longer declared
            log.error("%s: model %s is not declared; using %s", self.id, self.settings.model, self.base.model)
            model = self.office.model_for(self.base.model)
        assert model is not None  # the config file's model is validated at load
        return model

    def log(self, kind: str, status: str, **fields: Any) -> None:
        self.office.runlog.append(employee=self.id, kind=kind, status=status, **fields)

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ #
    # Incoming messages
    # ------------------------------------------------------------------ #

    async def _on_text(self, msg: Message[Any]) -> None:
        contact = msg.chat_info["contact"]
        cid: int = contact["contactId"]
        name: str = contact["profile"].get("displayName") or contact["localDisplayName"]
        text = (msg.text or "").strip()
        if text.startswith("/"):
            word, _, rest = text[1:].partition(" ")
            if word in ("admin", "ai", "forget"):
                await msg.reply(await self.command(cid, word, rest.strip(), by=name))
                return
        if text:
            await self._incoming(msg, cid, name, text, [])

    async def _on_other(self, msg: Message[Any]) -> None:
        """Images, files, voice, video and link previews: mirrored to the inbox with the
        preview SimpleX sends inline (the full file is not downloaded)."""
        contact = msg.chat_info["contact"]
        cid: int = contact["contactId"]
        name: str = contact["profile"].get("displayName") or contact["localDisplayName"]
        content: dict[str, Any] = dict(msg.content or {})  # type: ignore[call-overload]
        text = str(content.get("text") or "").strip()
        kind = {"image": "image", "video": "video", "voice": "audio", "file": "file", "link": "link"}.get(
            str(content.get("type")), "file"
        )
        att: dict[str, Any] = {"kind": kind}
        preview = content.get("image") or (content.get("preview") or {}).get("image")
        if isinstance(preview, str) and preview.startswith("data:image/") and len(preview) < 300_000:
            att["thumb"] = preview
        file = (msg.chat_item.get("chatItem") or {}).get("file") or {}
        if file.get("fileName"):
            att["name"] = str(file["fileName"])[:200]
        if kind == "link" and isinstance(link := (content.get("preview") or {}).get("uri"), str):
            att["url"], att["name"] = (
                link[:2000],
                str((content.get("preview") or {}).get("title") or link)[:200],
            )
        await self._incoming(msg, cid, name, text, [att])

    async def _incoming(
        self, msg: Message[Any], cid: int, name: str, text: str, attachments: list[dict[str, Any]]
    ) -> None:
        from .hub import customer_text

        conv, mid = self.office.hub.simplex_inbound(self, cid, name, text, attachments)
        if self.settings.paused or conv.mode == "human":
            return  # paused, or a person has taken this conversation over in the inbox
        prompt = customer_text([{"text": text, "attachments": attachments}])
        # Answer in the background so one slow reply doesn't block other chats;
        # the per-contact lock keeps each contact's replies in order.
        self._spawn(self._answer(msg, cid, name, prompt, conv.id, mid))

    async def _answer(
        self, msg: Message[Any], cid: int, name: str, text: str, conv_id: int, mid: int | None
    ) -> None:
        async with self._locks[cid]:
            if mid is not None and not self.office.hub.inbox.is_pending(conv_id, mid):
                return  # already answered (the start-up catch-up got to it first)
            try:
                answer = await self.agent.respond(cid, name, text)
            except Exception:
                log.exception("%s: failed to answer %s", self.id, name)
                self.log("reply", "error", contact=cid)
                return
            conv = self.office.hub.inbox.find(f"simplex:{self.id}", str(cid))
            if conv is not None and conv.mode == "human":
                return  # taken over while the AI was answering
            chunks = split_message(answer)
            await msg.reply(chunks[0])
            for chunk in chunks[1:]:
                await self.bot.api.api_send_text_message(["direct", cid], chunk)
            self.office.hub.simplex_outbound(self, cid, answer, "ai")

    async def notify_admins(self, text: str) -> int:
        sent = 0
        for cid in self.state.admins:
            try:
                await self.office.cluster.simplex_send(self, cid, text)  # from any shard
                sent += 1
            except Exception:
                log.exception("%s: cannot notify admin contact %s", self.id, cid)
        return sent

    # ------------------------------------------------------------------ #
    # Routines
    # ------------------------------------------------------------------ #

    def local_now(self, now: datetime | None = None) -> datetime:
        return (now or datetime.now(UTC)).astimezone(ZoneInfo(self.settings.timezone))

    def start_due_routines(self, now: datetime | None = None) -> list[asyncio.Task[RunResult]]:
        """Start every routine whose window is open and whose period has not run yet."""
        s = self.settings
        if s.paused:
            return []
        local = self.local_now(now)
        started = []
        for r in s.routines:
            if r.id in s.paused_routines or not r.in_window(local):
                continue
            key = r.period_key(local)
            if self.state.routine(r.id).get("period") == key:
                continue
            # Mark the period before the work, so a crash is not retried in a loop.
            self.state.update_routine(r.id, period=key, started=now_iso())
            started.append(self._spawn(self.run_routine(r, local)))
        return started

    async def run_routine(self, r: Routine, local: datetime, manual: bool = False) -> RunResult:
        try:
            res = await self.agent.run_routine(r, local)
        except Exception:
            log.exception("%s: routine %s failed", self.id, r.id)
            res = RunResult("Routine failed with an internal error.", "error")
        delivered = 0
        if r.deliver == "admins":
            delivered = await self.notify_admins(
                f"📋 *{self.settings.display_name} — {r.id}* ({local:%d/%m %H:%M})\n\n{res.text}"
            )
        self.state.update_routine(
            r.id, last_run=now_iso(), last_status=res.status, last_output=res.text[:4000], manual=manual
        )
        self.log("routine", res.status, routine=r.id, manual=manual, delivered=delivered, **res.log_fields())
        return res

    def describe_routines(self) -> str:
        s = self.settings
        if not s.routines:
            return "Nhân viên này chưa có lịch làm việc."
        local = self.local_now()
        lines = ["*Lịch làm việc*"]
        for r in s.routines:
            st = self.state.routine(r.id)
            nxt = r.next_start(local, st.get("period"))
            state = (
                "tạm dừng" if r.id in s.paused_routines else (f"lần tới {nxt:%d/%m %H:%M}" if nxt else "-")
            )
            lines.append(
                f"• {r.id}: {r.describe()} — {state}; lần chạy trước: {st.get('last_run', 'chưa')}"
                f" ({st.get('last_status', '-')})"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Chat commands
    # ------------------------------------------------------------------ #

    async def command(self, cid: int, word: str, args: str, by: str = "") -> str:
        if word == "forget":
            self.state.forget(cid)
            return "Đã xoá lịch sử trò chuyện của bạn. / Your conversation history was deleted."
        if word == "admin":
            token = self.base.admin_token
            if not token:
                return "Chức năng quản trị chưa được bật (thiếu admin_token)."
            if not hmac.compare_digest(args.encode(), token.encode()):
                log.warning("%s: wrong admin token from contact %s", self.id, cid)
                return "Mã quản trị không đúng."
            self.state.add_admin(cid)
            if by:
                self.state.remember_contact(cid, by)
            return f"Bạn đã là quản trị viên của {self.base.display_name}.\n\n{ADMIN_HELP}"
        if not self.state.is_admin(cid):
            return "Lệnh này chỉ dành cho quản trị viên. Gửi /admin <mã> để đăng nhập."
        return await self._admin(args, by=by or f"contact #{cid}")

    async def _admin(self, args: str, by: str) -> str:
        sub, _, rest = args.partition(" ")
        rest = rest.strip()
        s = self.settings
        st = self.state
        if sub in ("", "help"):
            return ADMIN_HELP
        if sub == "show":
            return (
                f"*{s.display_name}* ({s.id})\n"
                f"Trạng thái: {'tạm dừng' if s.paused else 'đang hoạt động'}\n"
                f"Model: {s.model} ({self.chat_model().profile.describe()}), "
                f"effort: {s.effort or 'mặc định'}\n"
                f"Skills: {', '.join(s.skills) or '(không có)'}\n"
                f"Được tự làm: {', '.join(s.releases) or '(không, mọi hành động chờ duyệt)'}\n"
                f"Lịch làm việc: {', '.join(r.id for r in s.routines) or '(không có)'}\n"
                f"Thay đổi so với file cấu hình: {', '.join(st.overrides) or '(không có)'}\n\n"
                f"*Prompt:*\n{s.system_prompt}"
            )
        if sub == "prompt":
            if not rest:
                return "Cú pháp: /ai prompt <nội dung>"
            st.set_override("system_prompt", rest)
            return "Đã cập nhật vai trò (system prompt)."
        if sub == "correct":
            if not rest:
                return "Cú pháp: /ai correct <quy tắc>"
            self.add_correction(rest)
            return f"Đã thêm quy tắc #{len(self.settings.corrections)}. Có hiệu lực từ tin nhắn tiếp theo."
        if sub == "corrections":
            return "*Quy tắc sửa sai*\n" + (
                "\n".join(f"{i}. ({c['date']}) {c['text']}" for i, c in enumerate(s.corrections, 1))
                or "(chưa có)"
            )
        if sub == "uncorrect":
            try:
                self.remove_correction(int(rest))
            except (ValueError, IndexError):
                return "Cú pháp: /ai uncorrect <số>, xem số bằng /ai corrections"
            return f"Đã xoá quy tắc #{rest}."
        if sub == "models":
            return "*Model AI đã khai báo*\n" + self.office.describe_models(current=s.model)
        if sub == "model":
            profile = self.office.model_profile(rest) if rest else None
            if profile is None:
                return "Cú pháp: /ai model <tên>. Các model đã khai báo:\n" + self.office.describe_models(
                    s.model
                )
            st.set_override("model", rest)
            return f"Đã gán model {rest} ({profile.describe()})."
        if sub == "effort":
            level = None if rest == "off" else rest
            if level is not None and level not in EFFORT_LEVELS:
                return f"Mức hợp lệ: {', '.join(EFFORT_LEVELS)}, off"
            st.set_override("effort", level)
            return f"Đã đặt effort: {rest}."
        if sub == "skills":
            return f"Đang bật: {', '.join(s.skills) or '(không có)'}\nCó sẵn: {', '.join(sk.available())}"
        if sub == "skill":
            action, _, name = rest.partition(" ")
            try:
                enabled = (
                    self.set_skill(name.strip(), action == "add") if action in ("add", "remove") else None
                )
            except KeyError as e:
                return str(e.args[0])
            if enabled is None:
                return "Cú pháp: /ai skill add|remove <tên>"
            return f"Skills: {', '.join(enabled) or '(không có)'}"
        if sub == "routines":
            return self.describe_routines()
        if sub == "run":
            r = s.routine(rest)
            if r is None:
                return f"Không có lịch '{rest}'. " + self.describe_routines()
            self._spawn(self.run_routine(r, self.local_now(), manual=True))
            return f"Đang chạy {r.id}; kết quả sẽ được gửi khi xong."
        if sub == "routine":
            action, _, rid = rest.partition(" ")
            if action not in ("pause", "resume") or s.routine(rid.strip()) is None:
                return "Cú pháp: /ai routine pause|resume <id>"
            self.set_routine_paused(rid.strip(), action == "pause")
            return f"Đã {'tạm dừng' if action == 'pause' else 'bật lại'} lịch {rid.strip()}."
        if sub == "pending":
            pending = self.actions.pending()
            if not pending:
                return "Không có yêu cầu nào chờ duyệt."
            return "*Chờ duyệt*\n" + "\n".join(
                f"#{a['id']} {a['action']} — {a.get('contact_name', '')}: "
                + ", ".join(f"{k}={v}" for k, v in a["args"].items())
                for a in pending
            )
        if sub in ("approve", "reject"):
            num, _, reason = rest.partition(" ")
            if not num.lstrip("#").isdigit():
                return f"Cú pháp: /ai {sub} <số>" + (" [lý do]" if sub == "reject" else "")
            if sub == "approve":
                return await self.actions.approve(int(num.lstrip("#")), by=by)
            return await self.actions.reject(int(num.lstrip("#")), by=by, reason=reason.strip())
        if sub == "releases":
            names = list(self.office.config.actions)
            return (
                f"Được tự làm (không cần duyệt): {', '.join(s.releases) or '(không có)'}\n"
                f"Hành động đã khai báo: {', '.join(names) or '(không có)'}"
            )
        if sub in ("release", "hold"):
            try:
                self.set_release(rest, sub == "release")
            except KeyError as e:
                return str(e.args[0])
            return (
                f"{rest} sẽ được thực hiện ngay, không cần duyệt."
                if sub == "release"
                else f"{rest} sẽ chờ duyệt trước khi thực hiện."
            )
        if sub in ("pause", "resume"):
            st.set_override("paused", sub == "pause")
            return "Đã tạm dừng mọi việc." if sub == "pause" else "Đã bật lại."
        if sub == "forget" and rest == "all":
            st.forget()
            return "Đã xoá toàn bộ trí nhớ hội thoại."
        if sub == "reset":
            st.clear_overrides()
            return "Đã quay về cấu hình trong file."
        return "Lệnh không hợp lệ.\n\n" + ADMIN_HELP

    # Settings changes shared by chat commands and the admin web UI.

    def add_correction(self, text: str) -> None:
        corrections = [
            *self.settings.corrections,
            {"date": f"{self.local_now():%Y-%m-%d}", "text": text.strip()},
        ]
        self.state.set_override("corrections", corrections)

    def remove_correction(self, number: int) -> None:
        corrections = list(self.settings.corrections)
        if not 1 <= number <= len(corrections):
            raise IndexError(number)
        del corrections[number - 1]
        self.state.set_override("corrections", corrections)

    def set_skill(self, name: str, enabled: bool) -> list[str]:
        skills = list(self.settings.skills)
        if enabled:
            if name not in sk.available():
                raise KeyError(f"Không có skill '{name}'. Có sẵn: {', '.join(sk.available())}")
            if name not in skills:
                skills.append(name)
        else:
            if name not in skills:
                raise KeyError(f"Skill '{name}' đang không bật.")
            skills.remove(name)
        self.state.set_override("skills", skills)
        return skills

    def set_release(self, action: str, released: bool) -> None:
        if action not in self.office.config.actions:
            raise KeyError(
                f"Không có hành động '{action}'. Đã khai báo: {', '.join(self.office.config.actions)}"
            )
        releases = [a for a in self.settings.releases if a != action] + ([action] if released else [])
        self.state.set_override("releases", releases)

    def set_routine_paused(self, routine_id: str, paused: bool) -> None:
        ids = [r for r in self.settings.paused_routines if r != routine_id] + ([routine_id] if paused else [])
        self.state.set_override("paused_routines", ids)


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split a long reply at paragraph, then line, then hard boundaries."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    chunks.append(text)
    return chunks


def prepare_skills(config: AppConfig) -> None:
    """Load plugin skills, register webhook actions as skills, and check every employee's list."""
    sk.load_plugins(config.plugins, config.plugin_paths)
    for name, action in config.actions.items():
        if name in sk.BUILTIN:
            raise ConfigError(f"action {name} has the same name as a built-in skill")
        sk.REGISTRY[name] = action.skill()
    for e in config.employees:
        sk.resolve(e.skills)  # fail fast on unknown skills


class Office:
    """All AI employees of one deployment, running in a single process."""

    def __init__(
        self,
        config: AppConfig,
        anthropic_llm: LLM | None = None,
        http: httpx2.AsyncClient | None = None,
        tick_seconds: float = 30.0,
    ):
        """`anthropic_llm` / `http` replace the network clients (used by tests)."""
        self.config = config
        self._anthropic_llm = anthropic_llm
        self._http = http
        self._models: dict[str, ChatModel] = {}
        self.tick_seconds = tick_seconds
        self._stopping = asyncio.Event()
        # database_url: one PostgreSQL for every process; otherwise SQLite files in state_dir
        self.db: Database | None = connect(config.database_url) if config.database_url else None
        self.office_db = self.db or Database(str(Path(config.state_dir) / "office.sqlite"))
        self.docs = DocStore(self.office_db)
        self._import_office_file(Path(config.state_dir) / "office.json")
        self.runlog = RunLog(Path(config.state_dir) / "runlog.jsonl", db=self.db)
        prepare_skills(config)
        self.employees: dict[str, Employee] = {
            e.id: Employee(e, self, config.state_dir) for e in config.employees
        }
        from .hub import ChannelHub

        self.hub = ChannelHub(self)
        from .cluster import Cluster

        self.cluster = Cluster(self)
        from .inventory import Inventory

        # products and stock live with the inbox (the office database, or inbox.db)
        self.inventory = Inventory(self.hub.inbox.db, self.docs)

    @property
    def http_client(self) -> httpx2.AsyncClient:
        if self._http is None:
            # Replies wait on model APIs for seconds: allow many requests in flight at once
            # (the default pool of 100 caps a process near 100 replies per second).
            limits = httpx2.Limits(max_connections=2000, max_keepalive_connections=200)
            self._http = httpx2.AsyncClient(timeout=60.0, limits=limits)
        return self._http

    # models: declared in the config, or added at runtime from the admin UI

    @property
    def runtime_models(self) -> dict[str, dict[str, Any]]:
        """Models added from the admin UI (API keys included): shared by all processes."""
        return self.docs.get("office", {}).get("models", {})

    def _import_office_file(self, path: Path) -> None:
        if path.exists():  # models added at runtime before they were kept in the database
            models = json.loads(path.read_text(encoding="utf-8")).get("models", {})
            self.docs.update("office", lambda d: d.setdefault("models", {}).update(models), {})
            path.rename(path.with_suffix(".json.imported"))

    def model_profile(self, name: str) -> ModelProfile | None:
        if name in self.runtime_models:
            return parse_models({name: self.runtime_models[name]})[name]
        return self.config.model_profile(name)

    def model_names(self) -> list[str]:
        return [*self.config.models, *(n for n in self.runtime_models if n not in self.config.models)]

    def add_runtime_model(self, name: str, raw: dict[str, Any]) -> ModelProfile:
        if name in self.config.models:
            raise ConfigError(f"model {name} is declared in the config file; edit it there")
        profile = parse_models({name: raw})[name]  # validates
        self.docs.update("office", lambda d: d.setdefault("models", {}).__setitem__(name, raw), {})
        self._models.pop(name, None)
        return profile

    def remove_runtime_model(self, name: str) -> None:
        if name not in self.runtime_models:
            raise KeyError(name)
        self.docs.update("office", lambda d: d.get("models", {}).pop(name, None), {})
        self._models.pop(name, None)

    def model_for(self, name: str) -> ChatModel | None:
        """One client per model, shared by the employees assigned to it."""
        cached = self._models.get(name)
        if cached is not None and name in self.runtime_models and cached.profile != self.model_profile(name):
            self._models.pop(name)  # changed from the admin UI, maybe by another process
        if name not in self._models:
            profile = self.model_profile(name)
            if profile is None:
                return None
            self._models[name] = make_model(profile, self._anthropic_llm, self._http)
        return self._models[name]

    def describe_models(self, current: str | None = None) -> str:
        names = self.model_names()
        if current and current not in names:
            names.append(current)  # a bare claude-* id in use
        lines = []
        for n in names:
            p = self.model_profile(n)
            if p is not None:
                users = [e.id for e in self.employees.values() if e.settings.model == n]
                lines.append(f"- {n}: {p.describe()}" + (f" — dùng bởi {', '.join(users)}" if users else ""))
        return "\n".join(lines) or "(chưa khai báo model nào)"

    def roster(self, exclude: str, allowed: list[str] | None = None) -> str:
        lines = []
        for e in self.employees.values():
            if e.id == exclude or (allowed is not None and e.id not in allowed):
                continue
            s = e.settings
            lines.append(f"- {e.id}: {s.display_name}" + (f" — {s.short_descr}" if s.short_descr else ""))
        return "\n".join(lines)

    # running

    def tick(self, now: datetime | None = None) -> list[asyncio.Task[RunResult]]:
        """One scheduler pass: start every due routine of every employee."""
        started: list[asyncio.Task[RunResult]] = []
        for e in self.employees.values():
            try:
                started += e.start_due_routines(now)
            except Exception:
                log.exception("%s: scheduler failed", e.id)
        return started

    async def _scheduler(self) -> None:
        while not self._stopping.is_set():
            self.tick()
            try:
                if changes := self.inventory.maybe_run_daily():
                    log.info("inventory: daily price run changed %d product(s)", len(changes))
            except Exception:
                log.exception("inventory: daily price run failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.tick_seconds)
            except TimeoutError:
                pass

    async def run(self) -> None:
        primary = self.cluster.is_primary  # other shards: web, webhooks and their conversations
        async with AsyncExitStack() as stack:
            for e in self.employees.values() if primary else ():
                await stack.enter_async_context(e.bot)
                log.info("%s (%s) address: %s", e.base.display_name, e.id, e.bot.address)
            if self.config.admin_ui:
                from .web import start_admin_ui

                runner = await start_admin_ui(self, self.config.admin_ui)
                stack.push_async_callback(runner.cleanup)
            await asyncio.gather(
                *([self._scheduler()] if primary else []),
                self.hub.run(self._stopping),
                self.cluster.run(self._stopping),
                *(e.bot.serve_forever() for e in self.employees.values() if primary),
            )

    def stop(self) -> None:
        self._stopping.set()
        for e in self.employees.values():
            e.bot.stop()
