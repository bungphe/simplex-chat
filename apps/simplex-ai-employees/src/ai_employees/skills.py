"""Skills: the tools an AI employee can use.

A skill is either a client tool (a Python function the agent loop runs) or a
server tool (declared to the API, run by Anthropic, e.g. web search).

Add your own skills in a plugin module and list it under `plugins:`:

    from ai_employees.skills import skill

    @skill("order_status", "Look up the status of an order by its number.",
           {"order_id": {"type": "string", "description": "Order number, e.g. DH-1024"}})
    async def order_status(ctx, order_id: str) -> str:
        return f"Order {order_id}: shipped"
"""

from __future__ import annotations

import importlib
import inspect
import re
import sys
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from .employee import Employee


class SkillError(Exception):
    """Raised by a skill to return an error result the model can see and recover from."""


@dataclass
class SkillContext:
    employee: Employee
    contact_id: int | None  # None when consulted by a colleague
    contact_name: str
    options: dict[str, Any] = field(default_factory=dict)
    consulting: bool = False  # True while answering a colleague (no further delegation)


Handler = Callable[..., "Awaitable[str] | str"]


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    input_schema: dict[str, Any] | None = None
    handler: Handler | None = None
    server_tool: dict[str, Any] | None = None
    # Internal skills read other contacts' data or office internals: offered only in
    # scheduled routines and to admins, never while serving an ordinary contact.
    internal: bool = False

    def openai_tool(self) -> dict[str, Any]:
        """Function-tool definition for OpenAI-compatible Chat Completions APIs.

        `additionalProperties` is left out: some compatible APIs (e.g. Gemini's) reject it.
        """
        params = {k: v for k, v in (self.input_schema or {}).items() if k != "additionalProperties"}
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": params},
        }

    def tool_param(self) -> dict[str, Any]:
        """Tool definition for the Anthropic Messages API."""
        if self.server_tool is not None:
            return dict(self.server_tool)
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "strict": True,
        }

    async def run(self, ctx: SkillContext, args: dict[str, Any]) -> str:
        assert self.handler is not None
        result = self.handler(ctx, **args)
        if inspect.isawaitable(result):
            result = await result
        return str(result)


REGISTRY: dict[str, Skill] = {}

# Config shortcuts: listing a group enables all its skills.
GROUPS: dict[str, tuple[str, ...]] = {"notes": ("remember", "recall")}


def skill(
    name: str, description: str, properties: dict[str, Any] | None = None, internal: bool = False
) -> Callable[[Handler], Handler]:
    """Register a client skill. Every property is required (strict tool schema)."""
    properties = properties or {}
    schema = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }

    def deco(fn: Handler) -> Handler:
        REGISTRY[name] = Skill(
            name=name, description=description, input_schema=schema, handler=fn, internal=internal
        )
        return fn

    return deco


def register_server_tool(name: str, description: str, tool: dict[str, Any]) -> None:
    REGISTRY[name] = Skill(name=name, description=description, server_tool=tool)


def expand(names: tuple[str, ...] | list[str]) -> list[str]:
    out: list[str] = []
    for n in names:
        for m in GROUPS.get(n, (n,)):
            if m not in out:
                out.append(m)
    return out


def resolve(names: tuple[str, ...] | list[str]) -> list[Skill]:
    unknown = [n for n in expand(names) if n not in REGISTRY]
    if unknown:
        raise KeyError(f"unknown skills: {', '.join(unknown)}; available: {', '.join(available())}")
    return [REGISTRY[n] for n in expand(names)]


def available() -> list[str]:
    return sorted([*REGISTRY, *GROUPS])


def load_plugins(modules: tuple[str, ...], paths: tuple[str, ...] = ()) -> None:
    for p in paths:
        if p not in sys.path:
            sys.path.insert(0, p)
    for m in modules:
        importlib.import_module(m)


# --------------------------------------------------------------------------- #
# Built-in skills
# --------------------------------------------------------------------------- #


@skill(
    "current_time",
    "Get the current date and time. Use it for anything that depends on today's date or time.",
    {"timezone": {"type": "string", "description": "IANA timezone, e.g. Asia/Ho_Chi_Minh"}},
)
def current_time(ctx: SkillContext, timezone: str) -> str:
    try:
        tz = ZoneInfo(timezone or ctx.employee.settings.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise SkillError(f"unknown timezone: {timezone}")
    return datetime.now(tz).strftime("%A %Y-%m-%d %H:%M %Z")


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


_WORD = re.compile(r"\w{2,}", re.UNICODE)


@skill(
    "knowledge_search",
    "Search the company's internal documents (products, prices, policies, procedures). "
    "Always search before answering questions about the company; quote what you find.",
    {"query": {"type": "string", "description": "Keywords to search for"}},
)
def knowledge_search(ctx: SkillContext, query: str) -> str:
    root = ctx.options.get("path")
    if not root or not Path(root).is_dir():
        raise SkillError("knowledge base is not configured")
    terms = set(_WORD.findall(_norm(query)))
    if not terms:
        raise SkillError("empty query")
    hits: list[tuple[int, str, str]] = []
    for f in sorted(Path(root).rglob("*")):
        if f.suffix.lower() not in (".md", ".txt") or not f.is_file():
            continue
        for chunk in re.split(r"\n\s*\n", f.read_text(encoding="utf-8")):
            words = set(_WORD.findall(_norm(chunk)))
            score = len(terms & words)
            if score:
                hits.append((score, str(f.relative_to(root)), chunk.strip()))
    if not hits:
        return "No matching documents."
    hits.sort(key=lambda h: -h[0])
    limit = int(ctx.options.get("max_results", 5))
    return "\n\n".join(f"[{name}]\n{text}" for _, name, text in hits[:limit])


@skill(
    "remember",
    "Save a fact about the current contact (name, preferences, order numbers...) to recall later.",
    {
        "key": {"type": "string", "description": "Short label, e.g. 'phone' or 'preferred_product'"},
        "value": {"type": "string", "description": "The fact to remember"},
    },
)
def remember(ctx: SkillContext, key: str, value: str) -> str:
    if ctx.contact_id is None:
        raise SkillError("no contact in this conversation")
    ctx.employee.state.set_note(ctx.contact_id, key, value)
    return f"Saved {key}."


@skill("recall", "List the facts saved about the current contact.")
def recall(ctx: SkillContext) -> str:
    if ctx.contact_id is None:
        raise SkillError("no contact in this conversation")
    notes = ctx.employee.state.notes(ctx.contact_id)
    return "\n".join(f"{k}: {v}" for k, v in notes.items()) or "Nothing saved yet."


@skill(
    "handoff_to_human",
    "Escalate to a human supervisor when you cannot help, the contact asks for a person, "
    "or the request needs approval. Tell the contact you have passed it on.",
    {"summary": {"type": "string", "description": "What the contact needs, with key details"}},
)
async def handoff_to_human(ctx: SkillContext, summary: str) -> str:
    sent = await ctx.employee.notify_admins(f"[Chuyển tiếp từ {ctx.contact_name}] {summary}")
    if not sent:
        raise SkillError("no human supervisor is available")
    return f"Forwarded to {sent} supervisor(s)."


@skill(
    "ask_colleague",
    "Ask another AI employee in the office a question in their area of expertise.",
    {
        "colleague": {"type": "string", "description": "Colleague id"},
        "question": {"type": "string", "description": "A complete, self-contained question"},
    },
)
async def ask_colleague(ctx: SkillContext, colleague: str, question: str) -> str:
    if ctx.consulting:
        raise SkillError("cannot delegate while answering a colleague")
    allowed = ctx.options.get("colleagues")
    if allowed is not None and colleague not in allowed:
        raise SkillError(f"unknown colleague {colleague}; ask one of: {', '.join(allowed)}")
    other = ctx.employee.office.employees.get(colleague)
    if other is None or other is ctx.employee:
        raise SkillError(f"unknown colleague: {colleague}")
    return await other.agent.consult(question, asker=ctx.employee.settings.display_name)


register_server_tool(
    "web_search",
    "Search the web (runs on Anthropic's servers).",
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 3},
)


@skill(
    "recent_conversations",
    "Read your own recent conversations with contacts, for summaries and follow-ups.",
    {"hours": {"type": "integer", "description": "How many hours back to read, e.g. 24"}},
    internal=True,
)
def recent_conversations(ctx: SkillContext, hours: int) -> str:
    since = datetime.now().astimezone() - timedelta(hours=max(1, min(int(hours), 24 * 31)))
    state = ctx.employee.state
    limit = int(ctx.options.get("max_chars", 12000))
    parts: list[str] = []
    for cid, name in state.contacts.items():
        turns = [
            t for t in state.timed_history(cid) if "ts" in t and datetime.fromisoformat(t["ts"]) >= since
        ]
        if turns:
            lines = [f"{'Contact' if t['role'] == 'user' else 'You'}: {t['content'][:400]}" for t in turns]
            parts.append(f"## {name} (contact #{cid}, {len(turns) // 2} exchanges)\n" + "\n".join(lines))
    if not parts:
        return f"No conversations in the last {hours} hours."
    text = "\n\n".join(parts)
    return text[:limit] + ("\n[truncated]" if len(text) > limit else "")


@skill(
    "office_report",
    "Report on every AI employee in the office: activity, errors, refusals, pending approvals "
    "and scheduled routine results. For supervising the team.",
    {"hours": {"type": "integer", "description": "How many hours back to report on, e.g. 24"}},
    internal=True,
)
def office_report(ctx: SkillContext, hours: int) -> str:
    office = ctx.employee.office
    stats = office.runlog.summary(hours=max(1, min(int(hours), 24 * 31)))
    lines = [f"Office report for the last {hours} hours:"]
    for e in office.employees.values():
        s = e.settings
        st = stats.get(e.id, {})
        status = "PAUSED" if s.paused else "active"
        lines.append(f"\n## {s.display_name} ({e.id}), {status}, model {s.model}")
        lines.append(
            f"work items: {st.get('total', 0)} {st.get('by_kind', {})}; outcomes: {st.get('by_status', {})}; "
            f"last activity: {st.get('last') or 'none'}"
        )
        pending = e.actions.pending()
        if pending:
            ids = ", ".join(f"#{a['id']} {a['action']}" for a in pending)
            lines.append(f"pending approvals: {ids}")
        for r in s.routines:
            rs = e.state.routine(r.id)
            paused = " (paused)" if r.id in s.paused_routines else ""
            lines.append(
                f"routine {r.id}{paused}: last run {rs.get('last_run', 'never')}, status {rs.get('last_status', '-')}"
            )
    return "\n".join(lines)


BUILTIN = frozenset(REGISTRY)
