"""An AI employee = one SimpleX account + an agent + chat-based admin commands."""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections import defaultdict
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx2
from simplex_chat import Bot, BotCommand, BotProfile, Message, SqliteDb

from . import skills as sk
from .agent import Agent
from .config import EFFORT_LEVELS, AppConfig, EmployeeConfig
from .llm import LLM
from .providers import ChatModel, make_model
from .state import EmployeeState

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 4000

ADMIN_HELP = """\
*Lệnh quản trị nhân viên AI*
/ai show — xem cấu hình hiện tại
/ai prompt <nội dung> — đặt vai trò / hướng dẫn (system prompt)
/ai models — xem các model AI đã khai báo
/ai model <tên> — gán model cho nhân viên này
/ai effort <low|medium|high|xhigh|max|off> — mức suy nghĩ
/ai skills — xem skill đang bật và skill có sẵn
/ai skill add <tên> — bật skill
/ai skill remove <tên> — tắt skill
/ai pause — tạm dừng tự động trả lời; /ai resume — bật lại
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
        self.state = EmployeeState(Path(state_dir) / f"{cfg.id}.json")
        self.agent = Agent(self)
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: set[asyncio.Task[None]] = set()
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
        if model is None:  # override names a model no longer declared in the config
            log.error("%s: model %s is not declared; using %s", self.id, self.settings.model, self.base.model)
            model = self.office.model_for(self.base.model)
        assert model is not None  # the config file's model is validated at load
        return model

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
                await msg.reply(self.command(cid, word, rest.strip()))
                return
        if self.settings.paused or not text:
            return
        # Answer in the background so one slow reply doesn't block other chats;
        # the per-contact lock keeps each contact's replies in order.
        task = asyncio.create_task(self._answer(msg, cid, name, text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _on_other(self, msg: Message[Any]) -> None:
        if not self.settings.paused:
            await msg.reply("Hiện mình chỉ đọc được tin nhắn văn bản. / I can only read text messages.")

    async def _answer(self, msg: Message[Any], cid: int, name: str, text: str) -> None:
        async with self._locks[cid]:
            try:
                answer = await self.agent.respond(cid, name, text)
            except Exception:
                log.exception("%s: failed to answer %s", self.id, name)
                return
            chunks = split_message(answer)
            await msg.reply(chunks[0])
            for chunk in chunks[1:]:
                await self.bot.api.api_send_text_message(["direct", cid], chunk)

    async def notify_admins(self, text: str) -> int:
        sent = 0
        for cid in self.state.admins:
            try:
                await self.bot.api.api_send_text_message(["direct", cid], text)
                sent += 1
            except Exception:
                log.exception("%s: cannot notify admin contact %s", self.id, cid)
        return sent

    # ------------------------------------------------------------------ #
    # Chat commands
    # ------------------------------------------------------------------ #

    def command(self, cid: int, word: str, args: str) -> str:
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
            return f"Bạn đã là quản trị viên của {self.base.display_name}.\n\n{ADMIN_HELP}"
        if not self.state.is_admin(cid):
            return "Lệnh này chỉ dành cho quản trị viên. Gửi /admin <mã> để đăng nhập."
        return self._admin(args)

    def _admin(self, args: str) -> str:
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
                f"Thay đổi so với file cấu hình: {', '.join(st.overrides) or '(không có)'}\n\n"
                f"*Prompt:*\n{s.system_prompt}"
            )
        if sub == "prompt":
            if not rest:
                return "Cú pháp: /ai prompt <nội dung>"
            st.set_override("system_prompt", rest)
            return "Đã cập nhật vai trò (system prompt)."
        if sub == "models":
            return "*Model AI đã khai báo*\n" + self.office.describe_models(current=s.model)
        if sub == "model":
            profile = self.office.config.model_profile(rest) if rest else None
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
            name = name.strip()
            enabled = list(s.skills)
            if action == "add":
                if name not in sk.available():
                    return f"Không có skill '{name}'. Có sẵn: {', '.join(sk.available())}"
                if name not in enabled:
                    enabled.append(name)
            elif action == "remove":
                if name not in enabled:
                    return f"Skill '{name}' đang không bật."
                enabled.remove(name)
            else:
                return "Cú pháp: /ai skill add|remove <tên>"
            st.set_override("skills", enabled)
            return f"Skills: {', '.join(enabled) or '(không có)'}"
        if sub in ("pause", "resume"):
            st.set_override("paused", sub == "pause")
            return "Đã tạm dừng tự động trả lời." if sub == "pause" else "Đã bật lại tự động trả lời."
        if sub == "forget" and rest == "all":
            st.forget()
            return "Đã xoá toàn bộ trí nhớ hội thoại."
        if sub == "reset":
            st.clear_overrides()
            return "Đã quay về cấu hình trong file."
        return "Lệnh không hợp lệ.\n\n" + ADMIN_HELP


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


class Office:
    """All AI employees of one deployment, running in a single process."""

    def __init__(
        self,
        config: AppConfig,
        anthropic_llm: LLM | None = None,
        http: httpx2.AsyncClient | None = None,
    ):
        """`anthropic_llm` / `http` replace the network clients (used by tests)."""
        self.config = config
        self._anthropic_llm = anthropic_llm
        self._http = http
        self._models: dict[str, ChatModel] = {}
        sk.load_plugins(config.plugins, config.plugin_paths)
        for e in config.employees:
            sk.resolve(e.skills)  # fail fast on unknown skills
        self.employees: dict[str, Employee] = {
            e.id: Employee(e, self, config.state_dir) for e in config.employees
        }

    def model_for(self, name: str) -> ChatModel | None:
        """One client per declared model, shared by the employees assigned to it."""
        if name not in self._models:
            profile = self.config.model_profile(name)
            if profile is None:
                return None
            self._models[name] = make_model(profile, self._anthropic_llm, self._http)
        return self._models[name]

    def describe_models(self, current: str | None = None) -> str:
        names = list(self.config.models)
        if current and current not in names:
            names.append(current)  # a bare claude-* id in use
        lines = []
        for n in names:
            p = self.config.model_profile(n)
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

    async def run(self) -> None:
        async with AsyncExitStack() as stack:
            for e in self.employees.values():
                await stack.enter_async_context(e.bot)
                log.info("%s (%s) address: %s", e.base.display_name, e.id, e.bot.address)
            await asyncio.gather(*(e.bot.serve_forever() for e in self.employees.values()))

    def stop(self) -> None:
        for e in self.employees.values():
            e.bot.stop()
