from __future__ import annotations

import pytest

from ai_employees import skills as sk
from ai_employees.config import ConfigError, load_config, parse_config
from ai_employees.employee import split_message
from ai_employees.state import EmployeeState

from fakes import EXAMPLES, ScriptedLLM, make_office


async def test_admin_login_and_runtime_config(tmp_path):
    sales = make_office(tmp_path, ScriptedLLM()).employees["sales"]

    assert "chỉ dành cho quản trị viên" in await sales.command(5, "ai", "show")
    assert await sales.command(5, "admin", "wrong") == "Mã quản trị không đúng."
    assert "Bạn đã là quản trị viên" in await sales.command(5, "admin", "secret-token")

    assert (
        await sales.command(5, "ai", "prompt Bạn là Lan.\nLuôn xưng em.")
        == "Đã cập nhật vai trò (system prompt)."
    )
    assert sales.settings.system_prompt == "Bạn là Lan.\nLuôn xưng em."
    assert "Đã gán model claude-sonnet-5" in await sales.command(5, "ai", "model claude-sonnet-5")
    assert "Mức hợp lệ" in await sales.command(5, "ai", "effort extreme")
    await sales.command(5, "ai", "effort high")
    assert "Không có skill" in await sales.command(5, "ai", "skill add teleport")
    await sales.command(5, "ai", "skill add web_search")
    await sales.command(5, "ai", "skill remove current_time")
    assert sales.settings.skills[-1] == "web_search" and "current_time" not in sales.settings.skills
    await sales.command(5, "ai", "pause")
    assert sales.settings.paused

    # overrides survive a restart
    reloaded = EmployeeState(sales.state.path, db=sales.state.db)
    assert reloaded.overrides["model"] == "claude-sonnet-5" and reloaded.is_admin(5)
    assert "claude-sonnet-5" in await sales.command(5, "ai", "show")

    assert await sales.command(5, "ai", "reset") == "Đã quay về cấu hình trong file."
    assert sales.settings.model == "claude-opus-5" and not sales.settings.paused


async def test_admin_disabled_without_token(tmp_path):
    sales = make_office(tmp_path, ScriptedLLM(), admin_token=None).employees["sales"]
    assert "chưa được bật" in await sales.command(1, "admin", "anything")


async def test_forget_deletes_own_history(tmp_path):
    sales = make_office(tmp_path, ScriptedLLM()).employees["sales"]
    sales.state.append_turn(1, "a", "b", keep=10)
    sales.state.append_turn(2, "c", "d", keep=10)
    await sales.command(1, "forget", "")
    assert sales.state.history(1) == [] and len(sales.state.history(2)) == 2


def test_example_config_and_plugin_skill(tmp_path):
    cfg = load_config(EXAMPLES / "employees.yaml")
    sk.load_plugins(cfg.plugins, cfg.plugin_paths)
    assert [e.id for e in cfg.employees] == ["sales", "accountant", "writer", "chief"]
    assert [e.model for e in cfg.employees] == ["claude", "gemini", "local", "claude"]
    assert [r.id for e in cfg.employees for r in e.routines] == [
        "morning-brief",
        "weekly-invoices",
        "end-of-day",
    ]
    assert list(cfg.actions) == ["create_order"] and cfg.admin_ui and cfg.admin_ui.port == 8080
    assert {m.provider for m in cfg.models.values()} == {"anthropic", "openai"}
    assert "order_status" in sk.expand(cfg.employees[0].skills)
    assert cfg.employees[0].skill_config["knowledge_search"]["path"].endswith("knowledge/sales")
    order = sk.REGISTRY["order_status"]
    assert order.tool_param()["input_schema"]["required"] == ["order_id"]


def test_config_validation(tmp_path):
    with pytest.raises(ConfigError, match="at least one"):
        parse_config({}, tmp_path)
    with pytest.raises(ConfigError, match="system_prompt"):
        parse_config({"employees": [{"id": "x", "display_name": "X"}]}, tmp_path)
    with pytest.raises(ConfigError, match="effort"):
        parse_config(
            {"employees": [{"id": "x", "display_name": "X", "system_prompt": "p", "effort": "huge"}]},
            tmp_path,
        )
    with pytest.raises(KeyError, match="unknown skills"):
        make_office(tmp_path, ScriptedLLM(), skills=["teleport"])


def test_server_tool_param():
    assert sk.REGISTRY["web_search"].tool_param() == {
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": 3,
    }


def test_split_message():
    assert split_message("ngắn") == ["ngắn"]
    parts = split_message("a" * 30 + "\n\n" + "b" * 30, limit=40)
    assert parts == ["a" * 30, "b" * 30]
    assert all(len(p) <= 40 for p in split_message("x" * 100, limit=40))
