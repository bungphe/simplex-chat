"""Provisioning: one product deployment per tenant.

`DockerComposeBackend` keeps a directory per tenant under tenants_dir/<slug>/ with a
generated data/employees.yaml (one AI sales employee, the admin UI, the web shop and the
plan's limits), a .env with the tenant's secrets, a docker-compose.yml pinning the
product image with its ports bound to 127.0.0.1, and a Caddy site snippet. Every shell
command goes through `run()`, so tests (and `backend: dry-run`) can record instead of
executing. Slugs are validated before they reach a path or a command line.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any, Protocol

import yaml

from .config import SLUG, SaasConfig

log = logging.getLogger(__name__)
APP_UID = "10001"  # the user the product image runs as (Dockerfile)


class ProvisionError(Exception):
    pass


class Backend(Protocol):
    def create(self, tenant: dict[str, Any], secrets: dict[str, str]) -> None: ...
    def start(self, tenant: dict[str, Any]) -> None: ...
    def stop(self, tenant: dict[str, Any]) -> None: ...
    def destroy(self, tenant: dict[str, Any], keep_backup: bool) -> str | None: ...
    def status(self, tenant: dict[str, Any]) -> str: ...
    def reset_admin_password(self, tenant: dict[str, Any], password: str) -> None: ...
    def usage(self, tenant: dict[str, Any]) -> dict[str, Any]: ...
    def backup_path(self, tenant: dict[str, Any]) -> Path | None: ...


def check_slug(slug: str) -> str:
    if not SLUG.match(slug or ""):
        raise ProvisionError(f"invalid slug {slug!r}")
    return slug


def run(cmd: list[str], timeout: float = 600) -> str:
    """Run a command (no shell); returns its output, raises ProvisionError when it fails."""
    log.info("provisioner: %s", " ".join(cmd))
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ProvisionError(f"{cmd[0]}: {e}") from None
    if done.returncode != 0:
        raise ProvisionError(f"{' '.join(cmd[:3])} failed ({done.returncode}): {done.stderr.strip()[-500:]}")
    return done.stdout


def employees_yaml(cfg: SaasConfig, tenant: dict[str, Any]) -> dict[str, Any]:
    """The tenant's product config: one sales employee, admin UI, web shop, plan limits."""
    plan = cfg.plan(tenant["plan"])
    limits = {k: v for k, v in plan.limits.items() if v is not None}
    shop = tenant["shop_name"]
    doc: dict[str, Any] = {
        "state_dir": "/data/state",
        "staff_language": tenant.get("lang") or "vi",
        "models": {"main": dict(cfg.tenant_model)},
        "admin_ui": {"host": "127.0.0.1", "port": 8080, "password_env": "AI_ADMIN_PASSWORD"},
        "storefront": {"host": "127.0.0.1", "port": 8081, "public_url": cfg.shop_url(tenant["slug"])},
        "defaults": {"model": "main", "timezone": "Asia/Ho_Chi_Minh", "admin_token_env": "AI_ADMIN_TOKEN"},
        "employees": [
            {
                "id": "sales",
                "display_name": "Tư vấn bán hàng",
                "short_descr": "Tư vấn sản phẩm, báo giá, đơn hàng",
                "db": "/data/sales",
                "welcome": f"Xin chào! Mình là trợ lý bán hàng của {shop}. Mình có thể giúp gì cho bạn?",
                "system_prompt": (
                    f"Bạn là nhân viên tư vấn bán hàng của {shop}. Tư vấn sản phẩm, báo giá theo bảng giá "
                    "trong kho hàng, kiểm tra đơn hàng, ghi nhận nhu cầu của khách. Không tự đặt ra giá hay "
                    "khuyến mãi. Khách muốn gặp người thật thì chuyển tiếp."
                ),
                "skills": ["products", "memory", "handoff_to_human", "current_time", "recent_conversations"],
            }
        ],
    }
    if limits:
        doc["limits"] = limits
    if cfg.smp_servers:
        doc["servers"] = {"smp": list(cfg.smp_servers)}
    return doc


def compose_yaml(cfg: SaasConfig, tenant: dict[str, Any]) -> dict[str, Any]:
    slug = check_slug(tenant["slug"])
    return {
        "services": {
            "app": {
                "image": cfg.image,
                "container_name": f"aie-{slug}",
                "restart": "unless-stopped",
                "env_file": ".env",
                "volumes": ["./data:/data"],
                "ports": [
                    f"127.0.0.1:{int(tenant['admin_port'])}:8080",
                    f"127.0.0.1:{int(tenant['shop_port'])}:8081",
                ],
            }
        }
    }


def caddy_snippet(cfg: SaasConfig, tenant: dict[str, Any]) -> str:
    """Host matchers for the wildcard site block of the main Caddyfile (see docs/SAAS.md)."""
    slug = check_slug(tenant["slug"])
    return (
        f"@t_{slug.replace('-', '_')}_admin host {slug}.{cfg.base_domain}\n"
        f"handle @t_{slug.replace('-', '_')}_admin {{\n\treverse_proxy 127.0.0.1:{int(tenant['admin_port'])}\n}}\n"
        f"@t_{slug.replace('-', '_')}_shop host {slug}-shop.{cfg.base_domain}\n"
        f"handle @t_{slug.replace('-', '_')}_shop {{\n\treverse_proxy 127.0.0.1:{int(tenant['shop_port'])}\n}}\n"
    )


def env_file(values: dict[str, str]) -> str:
    lines = []
    for k, v in values.items():
        if "\n" in v or "\r" in v:
            raise ProvisionError(f"{k}: a secret must not contain line breaks")
        lines.append(f"{k}={v}")
    return "\n".join(lines) + "\n"


class DockerComposeBackend:
    def __init__(self, cfg: SaasConfig, runner: Any = run):
        self.cfg = cfg
        self.run = runner
        self.tenants_dir = Path(cfg.tenants_dir)
        self.backups_dir = Path(cfg.backups_dir)

    def _dir(self, tenant: dict[str, Any]) -> Path:
        return self.tenants_dir / check_slug(tenant["slug"])

    def _compose(self, tenant: dict[str, Any], *args: str) -> str:
        return self.run(["docker", "compose", "-f", str(self._dir(tenant) / "docker-compose.yml"), *args])

    def _write_files(self, tenant: dict[str, Any]) -> None:
        d = self._dir(tenant)
        (d / "data" / "knowledge").mkdir(parents=True, exist_ok=True)
        (d / "data" / "employees.yaml").write_text(
            yaml.safe_dump(employees_yaml(self.cfg, tenant), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        (d / "docker-compose.yml").write_text(
            yaml.safe_dump(compose_yaml(self.cfg, tenant), sort_keys=False), encoding="utf-8"
        )
        if self.cfg.caddy_dir:
            Path(self.cfg.caddy_dir).mkdir(parents=True, exist_ok=True)
            (Path(self.cfg.caddy_dir) / f"{tenant['slug']}.caddy").write_text(
                caddy_snippet(self.cfg, tenant), encoding="utf-8"
            )

    def _write_env(self, tenant: dict[str, Any], values: dict[str, str]) -> None:
        path = self._dir(tenant) / ".env"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(env_file(values))
        os.chmod(path, 0o600)

    def _read_env(self, tenant: dict[str, Any]) -> dict[str, str]:
        path = self._dir(tenant) / ".env"
        values: dict[str, str] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                k, sep, v = line.partition("=")
                if sep and not k.startswith("#"):
                    values[k.strip()] = v
        return values

    def _reload_caddy(self) -> None:
        if self.cfg.caddy_dir and self.cfg.caddy_reload:
            self.run(list(self.cfg.caddy_reload))

    def create(self, tenant: dict[str, Any], secrets: dict[str, str]) -> None:
        d = self._dir(tenant)
        d.mkdir(parents=True, exist_ok=True)
        self._write_files(tenant)
        self._write_env(tenant, {**self.cfg.tenant_env, **secrets})
        # the container runs as uid 10001 and must own its data directory
        self.run(
            [
                "docker",
                "run",
                "--rm",
                "--user",
                "0",
                "-v",
                f"{d / 'data'}:/data",
                "--entrypoint",
                "chown",
                self.cfg.image,
                "-R",
                f"{APP_UID}:{APP_UID}",
                "/data",
            ]
        )
        self._compose(tenant, "up", "-d")
        self._reload_caddy()

    def start(self, tenant: dict[str, Any]) -> None:
        self._write_files(tenant)  # the plan (limits) may have changed
        self._compose(tenant, "up", "-d")
        self._reload_caddy()

    def stop(self, tenant: dict[str, Any]) -> None:
        self._compose(tenant, "stop")

    def restart(self, tenant: dict[str, Any]) -> None:
        self._compose(tenant, "up", "-d", "--force-recreate")

    def status(self, tenant: dict[str, Any]) -> str:
        d = self._dir(tenant)
        if not (d / "docker-compose.yml").exists():
            return "missing"
        out = self._compose(tenant, "ps", "--status", "running", "--services")
        return "running" if "app" in out.split() else "stopped"

    def reset_admin_password(self, tenant: dict[str, Any], password: str) -> None:
        values = self._read_env(tenant)
        values["AI_ADMIN_PASSWORD"] = password
        self._write_env(tenant, values)
        self.restart(tenant)

    def backup(self, tenant: dict[str, Any]) -> Path:
        """Archive the tenant's data directory (config, chat databases, state) into backups_dir."""
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        target = self.backups_dir / f"{tenant['slug']}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
        with tarfile.open(target, "w:gz") as tar:
            tar.add(self._dir(tenant) / "data", arcname="data")
        os.chmod(target, 0o600)
        return target

    def destroy(self, tenant: dict[str, Any], keep_backup: bool) -> str | None:
        d = self._dir(tenant)
        backup = None
        if keep_backup and (d / "data").exists():
            backup = str(self.backup(tenant))
        if (d / "docker-compose.yml").exists():
            self._compose(tenant, "down", "--volumes", "--remove-orphans")
        if self.cfg.caddy_dir:
            (Path(self.cfg.caddy_dir) / f"{tenant['slug']}.caddy").unlink(missing_ok=True)
            self._reload_caddy()
        if d.exists():
            # data/ belongs to uid 10001: remove it from a container, then the rest from here
            self.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--user",
                    "0",
                    "-v",
                    f"{d}:/t",
                    "--entrypoint",
                    "rm",
                    self.cfg.image,
                    "-rf",
                    "/t/data",
                ]
            )
            shutil.rmtree(d, ignore_errors=True)
        return backup

    def usage(self, tenant: dict[str, Any]) -> dict[str, Any]:
        total = 0
        data = self._dir(tenant) / "data"
        for root, _dirs, files in os.walk(data):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    pass
        return {"disk_mb": round(total / 1_000_000, 1)}

    def backup_path(self, tenant: dict[str, Any]) -> Path | None:
        if not self.backups_dir.exists():
            return None
        found = sorted(self.backups_dir.glob(f"{check_slug(tenant['slug'])}-*.tar.gz"))
        return found[-1] if found else None


class FakeBackend:
    """Records calls; for tests and for `backend: fake` (a control plane with no tenants running)."""

    def __init__(self, fail_first: int = 0):
        self.calls: list[tuple[str, str, Any]] = []
        self.running: set[str] = set()
        self.secrets: dict[str, dict[str, str]] = {}
        self.fail_first = fail_first

    def create(self, tenant: dict[str, Any], secrets: dict[str, str]) -> None:
        check_slug(tenant["slug"])
        self.calls.append(("create", tenant["slug"], dict(secrets)))
        if self.fail_first > 0:
            self.fail_first -= 1
            raise ProvisionError("simulated failure")
        self.secrets[tenant["slug"]] = dict(secrets)
        self.running.add(tenant["slug"])

    def start(self, tenant: dict[str, Any]) -> None:
        self.calls.append(("start", tenant["slug"], None))
        self.running.add(tenant["slug"])

    def stop(self, tenant: dict[str, Any]) -> None:
        self.calls.append(("stop", tenant["slug"], None))
        self.running.discard(tenant["slug"])

    def destroy(self, tenant: dict[str, Any], keep_backup: bool) -> str | None:
        self.calls.append(("destroy", tenant["slug"], keep_backup))
        self.running.discard(tenant["slug"])
        return f"/backups/{tenant['slug']}.tar.gz" if keep_backup else None

    def status(self, tenant: dict[str, Any]) -> str:
        return "running" if tenant["slug"] in self.running else "stopped"

    def reset_admin_password(self, tenant: dict[str, Any], password: str) -> None:
        self.calls.append(("reset_admin_password", tenant["slug"], None))
        self.secrets.setdefault(tenant["slug"], {})["AI_ADMIN_PASSWORD"] = password

    def usage(self, tenant: dict[str, Any]) -> dict[str, Any]:
        return {"disk_mb": 12.5}

    def backup_path(self, tenant: dict[str, Any]) -> Path | None:
        return None


def make_backend(cfg: SaasConfig) -> Backend:
    if cfg.backend == "fake":
        return FakeBackend()
    if cfg.backend == "dry-run":

        def dry(cmd: list[str], timeout: float = 0) -> str:
            log.info("dry-run: %s", " ".join(cmd))
            return ""

        return DockerComposeBackend(cfg, runner=dry)
    return DockerComposeBackend(cfg)
