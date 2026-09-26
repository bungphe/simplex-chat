"""Scripted stand-ins for the Claude API, shaped like SDK responses."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

from ai_employees.config import parse_config
from ai_employees.employee import Office

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def text(t: str, stop: str = "end_turn") -> NS:
    return NS(stop_reason=stop, content=[NS(type="text", text=t)])


def tool(name: str, input: dict[str, Any], id: str = "toolu_1") -> NS:
    return NS(stop_reason="tool_use", content=[NS(type="tool_use", id=id, name=name, input=input)])


class ScriptedLLM:
    """Returns queued responses in order; a callable entry is called with the request params."""

    def __init__(self, *responses: NS | Callable[[dict[str, Any]], NS]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **params: Any) -> NS:
        self.calls.append(params)
        r = self.responses.pop(0)
        return r(params) if callable(r) else r


def tool_names(params: dict[str, Any]) -> set[str]:
    return {t["name"] for t in params.get("tools", [])}


def tool_results(params: dict[str, Any]) -> list[dict[str, Any]]:
    last = params["messages"][-1]
    assert last["role"] == "user" and isinstance(last["content"], list)
    return last["content"]


def make_office(tmp_path: Path, llm: Any, smp: tuple[str, ...] = (), **sales_overrides: Any) -> Office:
    raw = {
        "state_dir": str(tmp_path / "state"),
        "servers": {"smp": list(smp)},
        "defaults": {"admin_token": "secret-token"},
        "employees": [
            {
                "id": "sales",
                "display_name": "Lan - Sales",
                "short_descr": "Tư vấn bán hàng",
                "db": str(tmp_path / "db" / "sales"),
                "system_prompt": "Bạn là Lan, nhân viên bán hàng.",
                "skills": ["knowledge_search", "notes", "ask_colleague", "handoff_to_human", "current_time"],
                "skill_config": {
                    "knowledge_search": {"path": str(EXAMPLES / "knowledge" / "sales")},
                    "ask_colleague": {"colleagues": ["accountant"]},
                },
                **sales_overrides,
            },
            {
                "id": "accountant",
                "display_name": "Minh - Kế toán",
                "db": str(tmp_path / "db" / "accountant"),
                "system_prompt": "Bạn là Minh, kế toán.",
                "skills": ["knowledge_search"],
                "skill_config": {"knowledge_search": {"path": str(EXAMPLES / "knowledge" / "accounting")}},
            },
        ],
    }
    return Office(parse_config(raw, base_dir=tmp_path), llm)
