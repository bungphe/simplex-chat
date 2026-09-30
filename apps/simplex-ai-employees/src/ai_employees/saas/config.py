"""saas.yaml: the control plane's own settings (plans, operators, domain, SMTP, backend)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import ConfigError, number

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,30}$")
RESERVED_SLUGS = frozenset({"www", "api", "app", "admin", "mail", "saas", "console", "portal", "static"})
PLAN_LIMIT_KEYS = ("users", "employees", "channels", "storage_mb")
STATUSES = ("pending_email", "trial", "active", "past_due", "suspended", "deleted")


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    price_month: int
    limits: dict[str, int | None]
    features: tuple[str, ...] = ()


@dataclass(frozen=True)
class Operator:
    username: str
    password: str


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int = 587
    tls: str = "starttls"
    user: str = ""
    password: str = ""
    sender: str = ""


@dataclass(frozen=True)
class SaasConfig:
    public_url: str  # this control plane's own address (https://saas.example.vn)
    base_domain: str  # tenants live at <slug>.<base_domain> and <slug>-shop.<base_domain>
    host: str = "127.0.0.1"
    port: int = 8090
    database_url: str = "./data/saas.sqlite"
    tenants_dir: str = "./tenants"
    backups_dir: str = "./backups"
    caddy_dir: str | None = None
    caddy_reload: tuple[str, ...] = ()
    image: str = "simplex-ai-employees"
    port_base: int = 20000
    backend: str = "docker"  # docker | dry-run | fake
    trial_days: int = 14
    grace_days: int = 7
    delete_after_days: int = 30
    currency: str = "VND"
    default_plan: str = "starter"
    plans: dict[str, Plan] = field(default_factory=dict)
    operators: tuple[Operator, ...] = ()
    smtp: SmtpConfig | None = None
    bank: dict[str, str] = field(default_factory=dict)  # transfer instructions shown on invoices
    tenant_env: dict[str, str] = field(default_factory=dict)  # written into every tenant's .env
    tenant_model: dict[str, Any] = field(default_factory=dict)  # the `models:` entry of a tenant
    smp_servers: tuple[str, ...] = ()
    session_hours: int = 12

    @property
    def https(self) -> bool:
        return self.public_url.startswith("https://")

    def plan(self, plan_id: str) -> Plan:
        if plan_id not in self.plans:
            raise ConfigError(f"unknown plan {plan_id!r}")
        return self.plans[plan_id]

    def admin_url(self, slug: str) -> str:
        return f"https://{slug}.{self.base_domain}"

    def shop_url(self, slug: str) -> str:
        return f"https://{slug}-shop.{self.base_domain}"


def load_saas_config(path: str | os.PathLike[str]) -> SaasConfig:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    return parse_saas_config(raw, base_dir=path.parent)


def parse_saas_config(raw: dict[str, Any], base_dir: Path) -> SaasConfig:
    def resolve(p: str) -> str:
        return str((base_dir / p).resolve()) if not os.path.isabs(p) else p

    public_url = str(raw.get("public_url") or "").rstrip("/")
    if not public_url.startswith(("https://", "http://")):
        raise ConfigError("public_url must start with https:// (http:// for local tests)")
    base_domain = str(raw.get("base_domain") or "").strip().lower().strip(".")
    if not re.fullmatch(r"[a-z0-9.-]+", base_domain or "-"):
        raise ConfigError("base_domain is required (e.g. aie.example.vn)")

    plans: dict[str, Plan] = {}
    for pid, p in (raw.get("plans") or {}).items():
        p = p or {}
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,20}", str(pid)):
            raise ConfigError(f"plan id {pid!r}: a-z, 0-9, - and _ only")
        limits = p.get("limits") or {}
        if unknown := set(limits) - set(PLAN_LIMIT_KEYS):
            raise ConfigError(f"plan {pid}: unknown limits {', '.join(sorted(unknown))}")
        plans[pid] = Plan(
            id=pid,
            name=str(p.get("name") or pid),
            price_month=number(p, "price_month", 0, f"plan {pid}: "),
            limits={
                k: (number(limits, k, None, f"plan {pid}: ") if limits.get(k) is not None else None)
                for k in PLAN_LIMIT_KEYS
            },
            features=tuple(str(f) for f in p.get("features") or ()),
        )
    if not plans:
        raise ConfigError("at least one plan is required under plans:")
    default_plan = str(raw.get("default_plan") or next(iter(plans)))
    if default_plan not in plans:
        raise ConfigError(f"default_plan {default_plan!r} is not under plans:")

    operators = []
    for i, o in enumerate(raw.get("operators") or []):
        o = o or {}
        username = str(o.get("username") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,31}", username):
            raise ConfigError(f"operators[{i}]: username is required (a-z, 0-9, . _ -)")
        password = os.environ.get(str(o.get("password_env") or ""), "") or str(o.get("password") or "")
        if not password:
            raise ConfigError(f"operator {username}: set the variable named in password_env")
        operators.append(Operator(username, password))

    smtp = None
    if s := raw.get("smtp"):
        if s.get("tls", "starttls") not in ("starttls", "ssl", "none"):
            raise ConfigError("smtp.tls: starttls, ssl or none")
        smtp = SmtpConfig(
            host=str(s.get("host") or ""),
            port=number(s, "port", 587, "smtp."),
            tls=str(s.get("tls", "starttls")),
            user=str(s.get("user") or ""),
            password=os.environ.get(str(s.get("password_env") or ""), ""),
            sender=str(s.get("sender") or s.get("user") or ""),
        )
        if not smtp.host or not smtp.sender:
            raise ConfigError("smtp needs host and sender")

    backend = str(raw.get("backend") or "docker")
    if backend not in ("docker", "dry-run", "fake"):
        raise ConfigError("backend: docker, dry-run or fake")
    reload_cmd = raw.get("caddy_reload") or ()
    if isinstance(reload_cmd, str):
        reload_cmd = reload_cmd.split()
    tenant_env: dict[str, str] = {}
    for k, v in (raw.get("tenant_env") or {}).items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", str(k)):
            raise ConfigError(f"tenant_env: {k!r} is not an environment variable name")
        v = str(v or "")
        if m := re.fullmatch(r"\$\{([A-Z][A-Z0-9_]*)\}", v):  # ${NAME}: this process's value
            v = os.environ.get(m.group(1), "")
        tenant_env[str(k)] = v
    tenant_model = dict(raw.get("tenant_model") or {"provider": "anthropic", "model": "claude-opus-5"})

    database_url = str(raw.get("database_url") or "")
    if env := raw.get("database_url_env"):
        database_url = os.environ.get(str(env), "") or database_url
    if not database_url:
        database_url = "./data/saas.sqlite"
    if not database_url.startswith(("postgres://", "postgresql://")):
        database_url = resolve(database_url.removeprefix("sqlite:///"))

    return SaasConfig(
        public_url=public_url,
        base_domain=base_domain,
        host=str(raw.get("host") or "127.0.0.1"),
        port=number(raw, "port", 8090),
        database_url=database_url,
        tenants_dir=resolve(str(raw.get("tenants_dir") or "./tenants")),
        backups_dir=resolve(str(raw.get("backups_dir") or "./backups")),
        caddy_dir=resolve(str(raw["caddy_dir"])) if raw.get("caddy_dir") else None,
        caddy_reload=tuple(str(x) for x in reload_cmd),
        image=str(raw.get("image") or "simplex-ai-employees"),
        port_base=number(raw, "port_base", 20000),
        backend=backend,
        trial_days=number(raw, "trial_days", 14),
        grace_days=number(raw, "grace_days", 7),
        delete_after_days=number(raw, "delete_after_days", 30),
        currency=str(raw.get("currency") or "VND"),
        default_plan=default_plan,
        plans=plans,
        operators=tuple(operators),
        smtp=smtp,
        bank={str(k): str(v) for k, v in (raw.get("bank") or {}).items()},
        tenant_env=tenant_env,
        tenant_model=tenant_model,
        smp_servers=tuple(str(s) for s in (raw.get("servers") or {}).get("smp") or ()),
        session_hours=number(raw, "session_hours", 12),
    )
