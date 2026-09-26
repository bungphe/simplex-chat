"""Configuration: a YAML file describes the office and its AI employees."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from .providers import PROVIDERS, ModelProfile, fallback_default

DEFAULT_MODEL = "claude-opus-5"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Fields an admin may change at runtime with `/ai ...`; persisted as overrides.
OVERRIDABLE = ("system_prompt", "model", "effort", "skills", "paused")


class ConfigError(Exception):
    pass


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
    skills: tuple[str, ...] = ()
    skill_config: dict[str, dict[str, Any]] = field(default_factory=dict)
    welcome: str | None = None
    short_descr: str | None = None
    admin_token: str | None = None
    timezone: str = "Asia/Ho_Chi_Minh"
    paused: bool = False

    def with_overrides(self, overrides: dict[str, Any]) -> EmployeeConfig:
        known = {k: v for k, v in overrides.items() if k in OVERRIDABLE}
        if "skills" in known:
            known["skills"] = tuple(known["skills"])
        return replace(self, **known)


@dataclass(frozen=True)
class AppConfig:
    employees: tuple[EmployeeConfig, ...]
    state_dir: str
    smp_servers: tuple[str, ...] = ()
    plugins: tuple[str, ...] = ()
    plugin_paths: tuple[str, ...] = ()
    models: dict[str, ModelProfile] = field(default_factory=dict)

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

        employees.append(
            EmployeeConfig(
                id=emp_id,
                display_name=merged["display_name"],
                db=resolve(merged.get("db") or f"./data/{emp_id}"),
                system_prompt=merged["system_prompt"].strip(),
                model=model,
                effort=effort,
                max_tokens=int(merged.get("max_tokens", 16000)),
                max_steps=int(merged.get("max_steps", 8)),
                history_messages=int(merged.get("history_messages", 40)),
                skills=tuple(merged.get("skills") or ()),
                skill_config=skill_config,
                welcome=merged.get("welcome"),
                short_descr=merged.get("short_descr"),
                admin_token=token,
                timezone=merged.get("timezone", "Asia/Ho_Chi_Minh"),
            )
        )
    if not employees:
        raise ConfigError("at least one employee is required")

    servers = raw.get("servers") or {}
    return AppConfig(
        employees=tuple(employees),
        state_dir=resolve(raw.get("state_dir") or "./data/state"),
        smp_servers=tuple(servers.get("smp") or ()),
        plugins=tuple(raw.get("plugins") or ()),
        plugin_paths=tuple(resolve(p) for p in raw.get("plugin_paths") or ()),
        models=models,
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
            timeout=float(m.get("timeout", 120.0)),
        )
    return models
