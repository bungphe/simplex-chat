"""Configuration: a YAML file describes the office and its AI employees."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .actions import ActionDef, parse_action
from .channels import ChannelConfig, parse_channel
from .i18n import LANGUAGES, normalize
from .providers import PROVIDERS, ModelProfile, fallback_default
from .routines import Routine, parse_routine

DEFAULT_MODEL = "claude-opus-5"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Fields an admin may change at runtime with `/ai ...`; persisted as overrides.
OVERRIDABLE = (
    "system_prompt",
    "model",
    "effort",
    "skills",
    "paused",
    "releases",
    "corrections",
    "paused_routines",
)
_TUPLES = ("skills", "releases", "corrections", "paused_routines")


class ConfigError(Exception):
    pass


def number(raw: dict[str, Any], key: str, default: Any, where: str = "", kind: type = int) -> Any:
    """A numeric setting, or a ConfigError naming it (`max_steps: eight` is a mistake to report)."""
    value = raw.get(key, default)
    try:
        if isinstance(value, bool):  # yes/no is not a number
            raise TypeError
        return kind(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}{key} must be a number, not {value!r}") from None


@dataclass(frozen=True)
class EmployeeConfig:
    id: str
    display_name: str
    db: str
    system_prompt: str
    model: str = DEFAULT_MODEL
    effort: str | None = "medium"
    max_tokens: int = 16000
    max_steps: int = 8
    history_messages: int = 40
    # Foreign customers: answer in the staff language (like the documents), then translate.
    # Recommended for small local models, which mistranslate facts when answering directly.
    translate_replies: bool = False
    # Model for translations (declared name); default: the employee's own model.
    translation_model: str | None = None
    skills: tuple[str, ...] = ()
    skill_config: dict[str, dict[str, Any]] = field(default_factory=dict)
    welcome: str | None = None
    short_descr: str | None = None
    admin_token: str | None = None
    timezone: str = "Asia/Ho_Chi_Minh"
    paused: bool = False
    routines: tuple[Routine, ...] = ()
    releases: tuple[str, ...] = ()  # actions this employee may perform without approval
    corrections: tuple[dict[str, str], ...] = ()  # dated rules from managers, set in chat or the UI
    paused_routines: tuple[str, ...] = ()

    def with_overrides(self, overrides: dict[str, Any]) -> EmployeeConfig:
        known = {k: v for k, v in overrides.items() if k in OVERRIDABLE}
        for k in _TUPLES:
            if k in known:
                known[k] = tuple(known[k])
        return replace(self, **known)

    def routine(self, routine_id: str) -> Routine | None:
        return next((r for r in self.routines if r.id == routine_id), None)


@dataclass(frozen=True)
class AdminUIConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    password: str | None = None


@dataclass(frozen=True)
class StorefrontConfig:
    """The public web shop (storefront.py): its own listener, meant for the Internet
    behind an HTTPS reverse proxy; public_url is used in links sent to customers."""

    host: str = "127.0.0.1"
    port: int = 8081
    public_url: str = ""


@dataclass(frozen=True)
class AppConfig:
    employees: tuple[EmployeeConfig, ...]
    state_dir: str
    smp_servers: tuple[str, ...] = ()
    plugins: tuple[str, ...] = ()
    plugin_paths: tuple[str, ...] = ()
    models: dict[str, ModelProfile] = field(default_factory=dict)
    actions: dict[str, ActionDef] = field(default_factory=dict)
    admin_ui: AdminUIConfig | None = None
    storefront: StorefrontConfig | None = None
    channels: tuple[ChannelConfig, ...] = ()
    # After downtime, customer messages up to this old still get an AI answer.
    catch_up_hours: float = 12.0
    # The language staff read and write: inbox translations and long-term summaries.
    staff_language: str = "vi"
    # PostgreSQL shared by several office processes; None: SQLite files in state_dir.
    database_url: str | None = None
    # Office processes sharing the database (cluster.shards); see cluster.py.
    shards: int = 1

    def model_profile(self, name: str) -> ModelProfile | None:
        """A declared model by name, or an implicit Claude model for a bare `claude-*` id."""
        if name in self.models:
            return self.models[name]
        if name.startswith("claude-"):
            return ModelProfile(
                name=name,
                provider="anthropic",
                model=name,
                refusal_fallback=fallback_default("anthropic", name),
            )
        return None


def load_config(path: str | os.PathLike[str]) -> AppConfig:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    return parse_config(raw, base_dir=path.parent)


def parse_config(raw: dict[str, Any], base_dir: Path) -> AppConfig:
    def resolve(p: str) -> str:
        return str((base_dir / p).resolve()) if not os.path.isabs(p) else p

    models = parse_models(raw.get("models") or {})
    actions: dict[str, ActionDef] = {}
    for name, a in (raw.get("actions") or {}).items():
        try:
            actions[name] = parse_action(name, a or {})
        except ValueError as e:
            raise ConfigError(str(e)) from None
    defaults = raw.get("defaults") or {}
    employees = []
    seen: set[str] = set()
    for i, e in enumerate(raw.get("employees") or []):
        merged = {**defaults, **e}
        emp_id = merged.get("id")
        if not emp_id or not isinstance(emp_id, str):
            raise ConfigError(f"employees[{i}]: 'id' is required")
        if emp_id in seen:
            raise ConfigError(f"duplicate employee id: {emp_id}")
        seen.add(emp_id)
        for req in ("display_name", "system_prompt"):
            if not merged.get(req):
                raise ConfigError(f"employee {emp_id}: '{req}' is required")
        model = merged.get("model", DEFAULT_MODEL)
        if model not in models and not str(model).startswith("claude-"):
            raise ConfigError(
                f"employee {emp_id}: model '{model}' is not declared under models: "
                f"({', '.join(models) or 'none declared'})"
            )
        tmodel = merged.get("translation_model")
        if tmodel and tmodel not in models and not str(tmodel).startswith("claude-"):
            raise ConfigError(
                f"employee {emp_id}: translation_model '{tmodel}' is not declared under models:"
            )
        effort = merged.get("effort", "medium")
        if effort is not None and effort not in EFFORT_LEVELS:
            raise ConfigError(f"employee {emp_id}: effort must be one of {EFFORT_LEVELS} or null")

        token = merged.get("admin_token")
        if env := merged.get("admin_token_env"):
            token = os.environ.get(env) or token

        skill_config = {name: dict(opts or {}) for name, opts in (merged.get("skill_config") or {}).items()}
        for opts in skill_config.values():
            if isinstance(opts.get("path"), str):
                opts["path"] = resolve(opts["path"])

        timezone = merged.get("timezone", "Asia/Ho_Chi_Minh")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError(f"employee {emp_id}: unknown timezone '{timezone}'") from None
        routines = []
        for r in merged.get("routines") or []:
            try:
                routines.append(parse_routine(r or {}))
            except ValueError as err:
                raise ConfigError(f"employee {emp_id}: {err}") from None
        if len({r.id for r in routines}) != len(routines):
            raise ConfigError(f"employee {emp_id}: duplicate routine id")
        releases = tuple(merged.get("releases") or ())
        if unknown := [a for a in releases if a not in actions]:
            raise ConfigError(f"employee {emp_id}: releases name unknown actions: {', '.join(unknown)}")

        employees.append(
            EmployeeConfig(
                id=emp_id,
                display_name=merged["display_name"],
                db=resolve(merged.get("db") or f"./data/{emp_id}"),
                system_prompt=merged["system_prompt"].strip(),
                model=model,
                effort=effort,
                max_tokens=number(merged, "max_tokens", 16000, f"employee {emp_id}: "),
                max_steps=number(merged, "max_steps", 8, f"employee {emp_id}: "),
                history_messages=number(merged, "history_messages", 40, f"employee {emp_id}: "),
                translate_replies=bool(merged.get("translate_replies", False)),
                translation_model=merged.get("translation_model") or None,
                skills=tuple(merged.get("skills") or ()),
                skill_config=skill_config,
                welcome=merged.get("welcome"),
                short_descr=merged.get("short_descr"),
                admin_token=token,
                timezone=timezone,
                routines=tuple(routines),
                releases=releases,
            )
        )
    if not employees:
        raise ConfigError("at least one employee is required")

    channels = []
    for c in raw.get("channels") or []:
        try:
            channels.append(parse_channel(c or {}, {e.id for e in employees}))
        except ValueError as e:
            raise ConfigError(str(e)) from None
    if len({c.id for c in channels}) != len(channels):
        raise ConfigError("duplicate channel id")

    staff_language = normalize(str(raw.get("staff_language") or "vi"))
    if staff_language is None:
        raise ConfigError(
            f"staff_language '{raw.get('staff_language')}' is not supported; use one of {', '.join(LANGUAGES)}"
        )
    servers = raw.get("servers") or {}
    return AppConfig(
        employees=tuple(employees),
        state_dir=resolve(raw.get("state_dir") or "./data/state"),
        smp_servers=tuple(servers.get("smp") or ()),
        plugins=tuple(raw.get("plugins") or ()),
        plugin_paths=tuple(resolve(p) for p in raw.get("plugin_paths") or ()),
        models=models,
        actions=actions,
        admin_ui=parse_admin_ui(raw.get("admin_ui")),
        storefront=parse_storefront(raw.get("storefront")),
        channels=tuple(channels),
        catch_up_hours=number(raw, "catch_up_hours", 12, kind=float),
        staff_language=staff_language,
        database_url=raw.get("database_url")
        or (os.environ.get(str(raw["database_url_env"])) if raw.get("database_url_env") else None)
        or None,
        shards=number(raw.get("cluster") or {}, "shards", 1, "cluster."),
    )


def parse_storefront(raw: dict[str, Any] | None) -> StorefrontConfig | None:
    if not raw:
        return None
    url = str(raw.get("public_url") or "").rstrip("/")
    if url and not url.startswith(("https://", "http://")):
        raise ConfigError("storefront.public_url must start with https://")
    return StorefrontConfig(
        host=os.environ.get("AI_STOREFRONT_HOST") or str(raw.get("host", "127.0.0.1")),
        port=number(raw, "port", 8081, "storefront."),
        public_url=url,
    )


def parse_admin_ui(raw: dict[str, Any] | None) -> AdminUIConfig | None:
    if not raw:
        return None
    password = raw.get("password")
    if env := raw.get("password_env"):
        password = os.environ.get(env) or password
    return AdminUIConfig(
        # AI_ADMIN_UI_HOST lets a container listen on 0.0.0.0 without editing the config
        host=os.environ.get("AI_ADMIN_UI_HOST") or str(raw.get("host", "127.0.0.1")),
        port=number(raw, "port", 8080, "admin_ui."),
        password=password,
    )


def parse_models(raw: dict[str, Any]) -> dict[str, ModelProfile]:
    """`models:` maps a name to {provider, model, base_url, api_key_env | api_key, ...}."""
    models = {}
    for name, m in raw.items():
        m = m or {}
        provider = m.get("provider", "anthropic")
        if provider not in PROVIDERS:
            raise ConfigError(f"model {name}: provider must be one of {PROVIDERS}")
        if not m.get("model"):
            raise ConfigError(f"model {name}: 'model' (the provider's model name) is required")
        unknown = set(m) - {
            "provider",
            "model",
            "base_url",
            "api_key",
            "api_key_env",
            "headers",
            "extra_body",
            "refusal_fallback",
            "timeout",
        }
        if unknown:
            raise ConfigError(f"model {name}: unknown fields {', '.join(sorted(unknown))}")
        models[name] = ModelProfile(
            name=name,
            provider=provider,
            model=m["model"],
            base_url=m.get("base_url"),
            api_key=m.get("api_key"),
            api_key_env=m.get("api_key_env"),
            headers={str(k): str(v) for k, v in (m.get("headers") or {}).items()},
            extra_body=dict(m.get("extra_body") or {}),
            refusal_fallback=bool(m.get("refusal_fallback", fallback_default(provider, m["model"]))),
            timeout=number(m, "timeout", 120.0, f"model {name}: ", float),
        )
    return models
