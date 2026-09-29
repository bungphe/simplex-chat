"""Operations: health checks, Prometheus metrics, backups and a watchdog.

- `GET /healthz` (no auth) on the admin UI and the web shop: 200 when the database answers.
- `GET /metrics` (admin UI, `Authorization: Bearer $AI_METRICS_TOKEN`; 404 while the variable
  is unset): process, run log, inbox, orders, channels, backups, disk and database gauges.
- `Backups`: a consistent copy of every SQLite database (the office's, the inbox's, the run
  log's, the SimpleX chat databases of every employee), `pg_dump` when the office runs on
  PostgreSQL, plus the JSON/YAML/knowledge files, in `backup-YYYYmmdd-HHMMSS.tar.gz`;
  nightly at AI_BACKUP_HOUR (office time), rotated, optionally handed to AI_BACKUP_COMMAND
  (`rclone copy {file} remote:bucket`) so copies leave the machine. `python -m ai_employees
  backup|restore` run the same code from the command line.
- `Watchdog` (every 5 minutes): disk nearly full, no recent backup, a channel in error,
  many model errors, PostgreSQL unreachable -> the admins of the first employee get one
  Vietnamese message per condition every 6 hours, and one when it clears.

Everything is configured by environment variables (see docs/OPERATIONS.md); the status of
the last backup and the alerts already sent live in the docs store ("ops_backup",
"ops_alerts"), so they survive restarts and are shared by every process.
"""

from __future__ import annotations

import asyncio
import hmac
import io
import json
import logging
import math
import os
import shlex
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from aiohttp import web

from .config import AppConfig
from .db import DocStore
from .i18n import tr
from .runlog import STATUSES
from .state import now_iso

if TYPE_CHECKING:
    from .employee import Office

log = logging.getLogger(__name__)

BACKUP_DOC = "ops_backup"  # {last_ok, last_error, last_file, size, last_day, last_attempt, first_seen}
ALERTS_DOC = "ops_alerts"  # condition -> {since, alerted, text, label}
DB_SUFFIXES = (".sqlite", ".db")
SIDE_SUFFIXES = ("-wal", "-shm", "-journal")
WATCH_SECONDS = 300
ALERT_REPEAT_SECONDS = 6 * 3600
BACKUP_STALE_SECONDS = 36 * 3600
BACKUP_RETRY_SECONDS = 3600  # after a failed nightly backup
DISK_MIN_FREE_BYTES = 2 * 1024**3
DISK_MIN_FREE_RATIO = 0.10
MODEL_ERRORS_MAX = 5  # per 15 minutes
PID_FILE = "office.pid"


def version() -> str:
    try:
        return metadata.version("simplex-ai-employees")
    except metadata.PackageNotFoundError:
        return "0.0.0"


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        log.warning("ops: %s is not a number; using %s", name, default)
        return default


def env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_pid(state_dir: str | Path) -> int | None:
    """The pid in state_dir/office.pid when that process is alive (same machine/container)."""
    path = Path(state_dir) / PID_FILE
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


class BackupError(Exception):
    pass


def _pg_command_url(url: str) -> tuple[str, dict[str, str]]:
    """The URL without its password (it would show in `ps`), and the password as PGPASSWORD."""
    parts = urlsplit(url)
    env = dict(os.environ)
    if parts.password is not None:
        env["PGPASSWORD"] = unquote(parts.password)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = (parts.username or "") + "@" + host + (f":{parts.port}" if parts.port else "")
        url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url, env


def _run(argv: list[str], env: dict[str, str] | None = None, timeout: float = 3600) -> None:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout, check=False)
    except FileNotFoundError:
        raise BackupError(f"{argv[0]}: command not found") from None
    except subprocess.TimeoutExpired:
        raise BackupError(f"{argv[0]}: timed out after {int(timeout)}s") from None
    if r.returncode:
        raise BackupError(f"{argv[0]} exited {r.returncode}: {(r.stderr or r.stdout).strip()[-500:]}")


def sqlite_copy(src: Path, dst: Path) -> list[Path]:
    """A consistent copy of a live SQLite database (the backup API waits out writers).
    A database Python cannot read (SQLCipher) is copied byte for byte with its WAL."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        source = sqlite3.connect(str(src), timeout=60)
        try:
            target = sqlite3.connect(str(dst))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        return [dst]
    except sqlite3.DatabaseError as e:
        log.warning("backup: %s is not a plain SQLite database (%s); copying the file", src.name, e)
        dst.unlink(missing_ok=True)
        out = [Path(shutil.copy2(src, dst))]
        for side in ("-wal", "-shm"):
            if (side_src := Path(str(src) + side)).exists():
                out.append(Path(shutil.copy2(side_src, str(dst) + side)))
        return out


def _is_db(path: Path) -> bool:
    return path.suffix in DB_SUFFIXES


def _is_side_file(path: Path) -> bool:
    return path.name.endswith(SIDE_SUFFIXES)


def _label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _safe_member(m: tarfile.TarInfo) -> bool:
    name = m.name
    if name.startswith("/") or ".." in Path(name).parts or m.issym() or m.islnk():
        return False
    return m.isfile() or m.isdir()


class Backups:
    """Backups of one office: what to copy, the archive, rotation, the upload, restore."""

    def __init__(
        self,
        config: AppConfig,
        docs: DocStore | None = None,
        *,
        backup_dir: str | Path | None = None,
        keep_days: int | None = None,
        keep_count: int | None = None,
        hour: int | None = None,
        after_command: str | None = None,
        enabled: bool | None = None,
        extras: list[str | Path] | None = None,
    ):
        self.config = config
        self.docs = docs
        self.state_dir = Path(config.state_dir)
        self.dir = Path(backup_dir or os.environ.get("AI_BACKUP_DIR") or self.state_dir / "backups")
        self.keep_days = keep_days if keep_days is not None else env_int("AI_BACKUP_KEEP_DAYS", 14)
        self.keep_count = keep_count if keep_count is not None else env_int("AI_BACKUP_KEEP_COUNT", 60)
        self.hour = hour if hour is not None else env_int("AI_BACKUP_HOUR", 3)
        self.after_command = (
            after_command if after_command is not None else os.environ.get("AI_BACKUP_COMMAND", "")
        )
        self.enabled = enabled if enabled is not None else env_flag("AI_BACKUP_ENABLED", True)
        # files outside state_dir to keep too (the config file); the knowledge folders are found
        self.extras: list[Path] = [Path(p) for p in extras or ()]
        self._status: dict[str, Any] = {}  # when there is no docs store
        self._lock = asyncio.Lock()
        # office time (the first employee's): the nightly hour and the "one per day" rule
        self.tz = ZoneInfo(config.employees[0].timezone) if config.employees else None

    def local_now(self) -> datetime:
        return datetime.now(UTC).astimezone(self.tz) if self.tz else datetime.now().astimezone()

    # status

    def status(self) -> dict[str, Any]:
        if self.docs is None:
            return dict(self._status)
        try:
            return dict(self.docs.get(BACKUP_DOC, {}) or {})
        except Exception:
            log.exception("backup: cannot read the status")
            return dict(self._status)

    def _set_status(self, **fields: Any) -> None:
        self._status.update(fields)
        if self.docs is not None:
            try:
                self.docs.update(BACKUP_DOC, lambda d: d.update(fields), {})
            except Exception:
                log.exception("backup: cannot save the status")

    def newest_file(self) -> Path | None:
        files = self.files()
        return files[-1] if files else None

    def files(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        return sorted(self.dir.glob("backup-*.tar.gz"), key=lambda p: p.name)

    def last_success(self) -> float | None:
        """When the last backup was made (this process, another one, or the CLI), as epoch seconds."""
        when = []
        if last_ok := self.status().get("last_ok"):
            try:
                when.append(datetime.fromisoformat(last_ok).timestamp())
            except ValueError:
                pass
        if newest := self.newest_file():
            try:
                when.append(newest.stat().st_mtime)
            except OSError:
                pass
        return max(when) if when else None

    def due(self, local: datetime, now: float | None = None) -> bool:
        """Nightly: once a day, from `hour` on (a late start still gets its backup)."""
        if not self.enabled or local.hour < self.hour:
            return False
        st = self.status()
        if st.get("last_day") == local.date().isoformat():
            return False
        last_attempt = st.get("last_attempt")
        if last_attempt:
            try:
                if (now or time.time()) - datetime.fromisoformat(
                    last_attempt
                ).timestamp() < BACKUP_RETRY_SECONDS:
                    return False
            except ValueError:
                pass
        return True

    # what goes in

    def sources(self) -> Iterator[tuple[str, Path]]:
        """(archive name, file) for everything to copy; SQLite files are recognised by suffix."""
        if self.state_dir.is_dir():
            for path in sorted(self.state_dir.rglob("*")):
                if not path.is_file() or path.is_relative_to(self.dir) or path.name == PID_FILE:
                    continue
                if _is_side_file(path) or path.name.endswith(".part"):
                    continue  # the SQLite copy is consistent by itself
                yield "state/" + path.relative_to(self.state_dir).as_posix(), path
        for e in self.config.employees:
            prefix = Path(e.db)
            if not prefix.parent.is_dir():
                continue
            for path in sorted(prefix.parent.glob(prefix.name + "_*.db")):
                yield f"employees/{e.id}/{path.name}", path

    def extra_sources(self) -> dict[str, Path]:
        """Archive prefix -> file or folder outside state_dir (config, knowledge)."""
        found: dict[str, Path] = {}
        seen: set[Path] = set()
        candidates = list(self.extras)
        for e in self.config.employees:
            kc = getattr(e, "skill_config", {}) or {}
            if path := (kc.get("knowledge_search") or {}).get("path"):
                candidates.append(Path(str(path)))
        for p in candidates:
            p = p.resolve()
            if p in seen or not p.exists() or p.is_relative_to(self.state_dir.resolve()):
                continue
            seen.add(p)
            found[f"extra/{len(found)}/{p.name}"] = p
        return found

    # the archive

    def run_sync(self) -> Path:
        """Write the archive (blocking: call from a thread), rotate, return its path."""
        self.dir.mkdir(parents=True, exist_ok=True)
        n = 0
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        stamp = self.local_now().strftime("%Y%m%d-%H%M%S")
        final = self.dir / f"backup-{stamp}.tar.gz"
        while final.exists():
            n += 1
            final = self.dir / f"backup-{stamp}-{n}.tar.gz"
        part = Path(str(final) + ".part")
        manifest: dict[str, Any] = {
            "created": now_iso(),
            "version": version(),
            "state_dir": str(self.state_dir),
            "postgres": bool(self.config.database_url),
            "employees": {e.id: str(Path(e.db)) for e in self.config.employees},
            "extras": {},
            "files": [],
        }
        try:
            with tempfile.TemporaryDirectory(dir=self.dir, prefix=".work-") as work_s:
                work = Path(work_s)
                with tarfile.open(part, "w:gz") as tar:
                    if self.config.database_url:
                        dump = work / "postgres.dump"
                        self._pg_dump(dump)
                        tar.add(dump, "postgres.dump")
                        manifest["files"].append("postgres.dump")
                        dump.unlink()
                    for arcname, path in self.sources():
                        if _is_db(path):
                            copy = work / "db" / arcname
                            for made in sqlite_copy(path, copy):
                                name = arcname + made.name[len(copy.name) :]
                                tar.add(made, name)
                                manifest["files"].append(name)
                                made.unlink()
                        else:
                            tar.add(path, arcname)
                            manifest["files"].append(arcname)
                    for prefix, path in self.extra_sources().items():
                        manifest["extras"][prefix] = str(path)
                        tar.add(path, prefix)
                    data = json.dumps(manifest, ensure_ascii=False, indent=1).encode()
                    info = tarfile.TarInfo("manifest.json")
                    info.size, info.mtime, info.mode = len(data), int(time.time()), 0o600
                    tar.addfile(info, io.BytesIO(data))
            os.chmod(part, 0o600)
            part.replace(final)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        self.rotate()
        return final

    def _pg_dump(self, dst: Path) -> None:
        assert self.config.database_url
        if not shutil.which("pg_dump"):
            raise BackupError("pg_dump not found: install postgresql-client (the Docker image has it)")
        url, env = _pg_command_url(self.config.database_url)
        _run(["pg_dump", "--format=custom", "--no-owner", "--file", str(dst), "--dbname", url], env)

    def _pg_restore(self, dump: Path) -> None:
        assert self.config.database_url
        if not shutil.which("pg_restore"):
            raise BackupError("pg_restore not found: install postgresql-client")
        url, env = _pg_command_url(self.config.database_url)
        _run(
            [
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--exit-on-error",
                "--dbname",
                url,
                str(dump),
            ],
            env,
        )

    def rotate(self) -> list[Path]:
        """Keep the newest `keep_count` files and none older than `keep_days` (always the newest)."""
        files = self.files()
        if not files:
            return []
        newest = files[-1]
        keep = set(files[-self.keep_count :]) if self.keep_count > 0 else set(files)
        cutoff = time.time() - self.keep_days * 86400 if self.keep_days > 0 else None
        removed = []
        for f in files:
            if f == newest:
                continue
            try:
                too_old = cutoff is not None and f.stat().st_mtime < cutoff
                if f not in keep or too_old:
                    f.unlink()
                    removed.append(f)
            except OSError:
                log.exception("backup: cannot remove %s", f)
        return removed

    def upload_sync(self, path: Path) -> None:
        """Run AI_BACKUP_COMMAND with {file} replaced (no shell)."""
        if not self.after_command.strip():
            return
        argv = [a.replace("{file}", str(path)) for a in shlex.split(self.after_command)]
        _run(argv, timeout=6 * 3600)

    async def run(self) -> Path:
        """Make a backup now (the work runs in a thread); status goes to the docs store."""
        async with self._lock:
            started = time.time()
            self._set_status(last_attempt=now_iso(), last_day=self.local_now().date().isoformat())
            try:
                path = await asyncio.to_thread(self.run_sync)
            except Exception as e:
                log.error("backup failed: %s", e)
                self._set_status(last_error=str(e)[:500])
                raise
            size = path.stat().st_size
            fields: dict[str, Any] = {
                "last_ok": now_iso(),
                "last_file": str(path),
                "size": size,
                "duration_s": round(time.time() - started, 1),
                "last_error": None,
            }
            try:
                await asyncio.to_thread(self.upload_sync, path)
                fields["upload_error"] = None
            except Exception as e:  # noqa: BLE001 - the archive exists; the upload failure is reported
                log.error("backup: after_command failed: %s", e)
                fields["upload_error"] = str(e)[:500]
                fields["last_error"] = f"after_command: {e}"[:500]
            self._set_status(**fields)
            log.info("backup: %s (%.1f MB)", path, size / 1e6)
            return path

    # restore

    def restore(self, archive: Path, *, extras: bool = False) -> list[str]:
        """Put an archive back (blocking; the office must not be running). The current
        state_dir moves to `<state_dir>.before-restore-<ts>`, replaced files keep a copy
        with the same suffix. Returns notes for the operator."""
        archive = Path(archive)
        notes: list[str] = []
        stamp = self.local_now().strftime("%Y%m%d-%H%M%S")
        suffix = f".before-restore-{stamp}"
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            bad = [m.name for m in members if not _safe_member(m)]
            if bad:
                raise BackupError(f"refusing archive with unsafe entries: {bad[:3]}")
            names = {m.name for m in members}
            if "manifest.json" not in names:
                raise BackupError("not a backup made by ai_employees (no manifest.json)")
            manifest = json.loads(tar.extractfile("manifest.json").read())  # type: ignore[union-attr]

            def put(m: tarfile.TarInfo, target: Path) -> None:
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    return
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(m)
                assert src is not None
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(target, 0o600)

            def aside(path: Path) -> None:
                for p in [path, *(Path(str(path) + s) for s in SIDE_SUFFIXES)]:
                    if p.exists():
                        p.rename(Path(str(p) + suffix))

            # 1. state_dir
            if self.state_dir.exists():
                moved = Path(str(self.state_dir) + suffix)
                self.state_dir.rename(moved)
                notes.append(f"previous state moved to {moved}")
            self.state_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.state_dir, 0o700)
            for m in members:
                if m.name.startswith("state/") and m.name != "state/":
                    put(m, self.state_dir / m.name[len("state/") :])
            # 2. SimpleX databases of the employees (by id, into the configured location)
            prefixes = {e.id: Path(e.db) for e in self.config.employees}
            for m in members:
                if not m.name.startswith("employees/") or not m.isfile():
                    continue
                _, emp, name = m.name.split("/", 2)
                prefix = prefixes.get(emp)
                if prefix is None:
                    old = manifest.get("employees", {}).get(emp)
                    if not old:
                        notes.append(f"skipped {m.name}: employee {emp} is not in the config")
                        continue
                    prefix = Path(old)
                target = prefix.parent / name
                if not name.endswith(SIDE_SUFFIXES):
                    aside(target)
                put(m, target)
            # 3. extras (config, knowledge) only on request
            if manifest.get("extras"):
                if extras:
                    for prefix_name, original in manifest["extras"].items():
                        base = Path(original)
                        for m in members:
                            if m.name == prefix_name or m.name.startswith(prefix_name + "/"):
                                target = (
                                    base / m.name[len(prefix_name) + 1 :] if m.name != prefix_name else base
                                )
                                if m.name == prefix_name and target.exists() and target.is_file():
                                    aside(target)
                                put(m, target)
                        notes.append(f"restored {original}")
                else:
                    notes.append("config and knowledge files were not restored (add --extras)")
            # 4. PostgreSQL
            if "postgres.dump" in names:
                if self.config.database_url:
                    dump = self.state_dir / "postgres.dump"
                    put(next(m for m in members if m.name == "postgres.dump"), dump)
                    try:
                        self._pg_restore(dump)
                        notes.append("PostgreSQL restored with pg_restore")
                    finally:
                        dump.unlink(missing_ok=True)
                else:
                    put(
                        next(m for m in members if m.name == "postgres.dump"),
                        self.state_dir / "postgres.dump",
                    )
                    notes.append("the config has no database_url: postgres.dump left in state_dir")
            elif self.config.database_url:
                notes.append("the archive has no PostgreSQL dump: the database was not changed")
        return notes


class Watchdog:
    """Conditions worth waking an admin for, with one message per condition per 6 hours."""

    def __init__(self, office: Office, ops: Ops, now: Callable[[], float] = time.time):
        self.office = office
        self.ops = ops
        self.now = now
        self.repeat = ALERT_REPEAT_SECONDS
        self._state: dict[str, dict[str, Any]] | None = None

    def state(self) -> dict[str, dict[str, Any]]:
        if self._state is None:
            try:
                self._state = dict(self.office.docs.get(ALERTS_DOC, {}) or {})
            except Exception:  # noqa: BLE001 - the database may be the very thing that is down
                self._state = {}
        return self._state

    def _save(self) -> None:
        state = self.state()
        try:
            self.office.docs.update(ALERTS_DOC, lambda d: (d.clear(), d.update(state)), {})
        except Exception:  # noqa: BLE001 - the database may be the very thing that is down
            log.warning("watchdog: cannot save the alert state (kept in memory)")

    def conditions(self, snap: dict[str, Any]) -> dict[str, tuple[str, str]]:
        """Active conditions: key -> (alert text, short label for the recovery message)."""
        found: dict[str, tuple[str, str]] = {}
        free, total = snap.get("disk_free"), snap.get("disk_total")
        if free is not None and total and (free < DISK_MIN_FREE_BYTES or free / total < DISK_MIN_FREE_RATIO):
            found["disk"] = (
                tr(
                    "Ổ đĩa sắp đầy: chỉ còn {0} GB trống ({1}%) tại {2}",
                    f"{free / 1024**3:.1f}",
                    f"{100 * free / total:.0f}",
                    self.office.config.state_dir,
                ),
                tr("dung lượng ổ đĩa"),
            )
        if self.ops.backups.enabled:
            age = snap.get("backup_age_s")
            if age is not None and age > BACKUP_STALE_SECONDS:
                err = (snap.get("backup") or {}).get("last_error")
                found["backup"] = (
                    tr("Không có bản sao lưu mới nào trong {0} giờ qua", int(age // 3600))
                    + (tr(" — lỗi gần nhất: {0}", err) if err else ""),
                    tr("sao lưu tự động"),
                )
        for cid, err in (snap.get("channel_errors") or {}).items():
            found[f"channel:{cid}"] = (tr("Kênh {0} đang lỗi: {1}", cid, err), tr("kênh {0}", cid))
        errors = snap.get("model_errors_15m") or 0
        if errors > MODEL_ERRORS_MAX:
            found["model_errors"] = (tr("{0} lỗi gọi model AI trong 15 phút qua", errors), tr("gọi model AI"))
        if self.office.db is not None and snap.get("db_ok") is False:
            found["postgres"] = (
                tr("Không kết nối được PostgreSQL: {0}", snap.get("db_error") or "?"),
                tr("kết nối PostgreSQL"),
            )
        return found

    async def check(self) -> list[str]:
        """One pass: send what is due, remember it; returns the messages sent."""
        snap = await asyncio.to_thread(self.ops.snapshot)
        active = self.conditions(snap)
        state = self.state()
        now = self.now()
        sent: list[str] = []
        for key, (text, label) in active.items():
            prev = state.get(key)
            if prev is not None and now - float(prev.get("alerted", 0)) < self.repeat:
                continue
            msg = tr("⚠️ Cảnh báo vận hành: {0}", text)
            if await self._notify(msg):
                sent.append(msg)
            state[key] = {"since": prev.get("since") if prev else now_iso(), "alerted": now, "label": label}
        for key in [k for k in state if k not in active]:
            msg = tr("✅ Đã ổn lại: {0}", state[key].get("label") or key)
            if await self._notify(msg):
                sent.append(msg)
            state.pop(key)
        if sent:
            self._save()
        return sent

    async def _notify(self, text: str) -> bool:
        employee = next(iter(self.office.employees.values()), None)
        if employee is None:
            log.warning("watchdog: %s (no employee to notify admins through)", text)
            return True
        try:
            n = await employee.notify_admins(text)
        except Exception:
            log.exception("watchdog: cannot notify admins")
            return False
        if not n:
            log.warning("watchdog: %s (no admin contact linked to %s)", text, employee.id)
        return True


class Ops:
    """The office's operations: routes, the nightly backup and the watchdog loop."""

    def __init__(self, office: Office, interval: float = WATCH_SECONDS):
        self.office = office
        self.interval = interval
        self.started = time.time()
        self.backups = Backups(office.config, office.docs)
        self.watchdog = Watchdog(office, self)
        self.pid_file = Path(office.config.state_dir) / PID_FILE

    # routes

    def add_routes(self, r: web.UrlDispatcher) -> None:
        """The admin app: /healthz (public), /metrics (token), /api/ops (logged-in admins)."""
        r.add_get("/healthz", self.healthz)
        r.add_get("/metrics", self.metrics)
        r.add_get("/api/ops", self.api_status)

    def health_routes(self, r: web.UrlDispatcher) -> None:
        """The web shop: /healthz only."""
        r.add_get("/healthz", self.healthz)

    def local_now(self) -> datetime:
        return self.backups.local_now()

    def uptime(self) -> float:
        return time.time() - self.started

    def db_check(self) -> tuple[bool, str | None]:
        """One `SELECT 1` on the office database (blocking, short)."""
        try:
            self.office.office_db.row("SELECT 1 AS ok")
            return True, None
        except Exception as e:  # noqa: BLE001 - whatever failed, the office is unhealthy
            return False, f"{type(e).__name__}: {str(e)[:200]}"

    async def healthz(self, _request: web.Request) -> web.Response:
        try:
            ok, err = await asyncio.wait_for(asyncio.to_thread(self.db_check), timeout=5)
        except TimeoutError:
            ok, err = False, "timeout"
        if not ok:
            log.warning("healthz: database check failed: %s", err)
        body = {
            "ok": ok,
            "version": version(),
            "db": "ok" if ok else "error",
            "employees": len(self.office.employees),
            "uptime_s": int(self.uptime()),
        }
        return web.json_response(body, status=200 if ok else 503, headers={"Cache-Control": "no-store"})

    async def metrics(self, request: web.Request) -> web.Response:
        token = os.environ.get("AI_METRICS_TOKEN", "")
        if not token:
            raise web.HTTPNotFound()
        auth = request.headers.get("Authorization", "")
        given = auth[7:].strip() if auth.startswith("Bearer ") else ""
        if not given or not hmac.compare_digest(given, token):
            raise web.HTTPUnauthorized(headers={"WWW-Authenticate": "Bearer"})
        text = await asyncio.to_thread(self.metrics_text)
        return web.Response(
            body=text.encode("utf-8"),
            headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8", "Cache-Control": "no-store"},
        )

    async def api_status(self, _request: web.Request) -> web.Response:
        snap = await asyncio.to_thread(self.snapshot)
        snap["alerts"] = self.watchdog.state()
        snap["backup_dir"] = str(self.backups.dir)
        snap["backup_enabled"] = self.backups.enabled
        snap["backup_hour"] = self.backups.hour
        snap["version"] = version()
        return web.json_response(snap, dumps=lambda d: json.dumps(d, ensure_ascii=False, default=str))

    # measurements (blocking, short; run in a thread)

    def snapshot(self) -> dict[str, Any]:
        o = self.office
        snap: dict[str, Any] = {"uptime_s": int(self.uptime()), "employees": len(o.employees)}
        snap["db_ok"], snap["db_error"] = self.db_check()

        def part(name: str, fn: Callable[[], Any], default: Any = None) -> None:
            try:
                snap[name] = fn()
            except Exception as e:  # noqa: BLE001 - one missing gauge must not hide the others
                log.debug("ops: %s unavailable: %s", name, e)
                snap[name] = default

        since = (datetime.now().astimezone() - timedelta(minutes=15)).isoformat(timespec="seconds")
        part(
            "runs_15m",
            lambda: {
                r["status"]: int(r["n"])
                for r in o.runlog.db.rows(
                    "SELECT status, COUNT(*) AS n FROM runlog WHERE ts>=? GROUP BY status", (since,)
                )
            },
            {},
        )
        snap["model_errors_15m"] = (snap.get("runs_15m") or {}).get("error", 0)
        inbox_db = o.hub.inbox.db
        part(
            "inbox_unread",
            lambda: int((inbox_db.row("SELECT COALESCE(SUM(unread), 0) AS n FROM conversations") or {})["n"]),
        )
        part(
            "orders_open",
            lambda: int(
                (
                    inbox_db.row(
                        "SELECT COUNT(*) AS n FROM inv_orders WHERE status NOT IN ('completed', 'cancelled')"
                    )
                    or {}
                )["n"]
            ),
        )
        part(
            "channel_errors",
            lambda: {
                cid: str(err)[:200]
                for cid in o.hub.channels
                if (err := o.hub.inbox.channel_state(cid).get("last_error"))
            },
            {},
        )

        def disk() -> tuple[int, int]:
            u = shutil.disk_usage(o.config.state_dir)
            return u.free, u.total

        try:
            snap["disk_free"], snap["disk_total"] = disk()
        except OSError:
            snap["disk_free"] = snap["disk_total"] = None
        part("db_size", self.db_size)
        part("backup", self.backups.status, {})
        last = self.backups.last_success()
        st = snap.get("backup") or {}
        if last is None and not st.get("first_seen"):
            # nothing yet: count the 36 hours from now on, not from the dawn of time
            self.backups._set_status(first_seen=now_iso())
            st = self.backups.status()
        if last is None and st.get("first_seen"):
            try:
                last = datetime.fromisoformat(st["first_seen"]).timestamp()
            except ValueError:
                last = None
        snap["backup_age_s"] = max(0.0, time.time() - last) if last is not None else None
        snap["backup_ok"] = bool(st.get("last_ok")) and not st.get("last_error")
        return snap

    def db_size(self) -> int:
        o = self.office
        if o.db is not None:
            row = o.db.row("SELECT pg_database_size(current_database()) AS n")
            return int(row["n"]) if row else 0
        total = 0
        for path in Path(o.config.state_dir).rglob("*"):
            if path.is_file() and (_is_db(path) or path.name.endswith("-wal")):
                total += path.stat().st_size
        return total

    def metrics_text(self) -> str:
        s = self.snapshot()
        out: list[str] = []

        def gauge(name: str, value: Any, help_: str, labels: dict[str, str] | None = None) -> None:
            if value is None:
                return
            if isinstance(value, float) and math.isnan(value):
                return
            if not any(line.startswith(f"# TYPE {name} ") for line in out):
                out.append(f"# HELP {name} {help_}")
                out.append(f"# TYPE {name} gauge")
            lab = "{" + ",".join(f'{k}="{_label(v)}"' for k, v in labels.items()) + "}" if labels else ""
            out.append(f"{name}{lab} {int(value) if isinstance(value, bool) else value}")

        gauge("aie_info", 1, "Version of the office.", {"version": version()})
        gauge("aie_up", s["db_ok"], "1 when the office database answers.")
        gauge("aie_uptime_seconds", s["uptime_s"], "Seconds since this process started.")
        gauge("aie_employees", s["employees"], "AI employees configured.")
        runs = s.get("runs_15m") or {}
        for status in STATUSES:
            gauge(
                "aie_runs_15m",
                runs.get(status, 0),
                "Run log entries in the last 15 minutes.",
                {"status": status},
            )
        gauge(
            "aie_model_errors_15m",
            s.get("model_errors_15m", 0),
            "Runs that ended in error in the last 15 minutes.",
        )
        gauge("aie_inbox_unread", s.get("inbox_unread"), "Unread customer messages in the shared inbox.")
        gauge("aie_orders_open", s.get("orders_open"), "Orders neither completed nor cancelled.")
        errors = s.get("channel_errors") or {}
        gauge("aie_channel_errors", len(errors), "Chat channels whose last poll or send failed.")
        for cid in self.office.hub.channels:
            gauge(
                "aie_channel_error",
                1 if cid in errors else 0,
                "1 when this channel is in error.",
                {"channel": cid},
            )
        st = s.get("backup") or {}
        gauge("aie_backup_ok", s.get("backup_ok"), "1 when the last backup succeeded.")
        if st.get("last_ok") or self.backups.newest_file():
            gauge(
                "aie_backup_age_seconds", s.get("backup_age_s"), "Seconds since the last successful backup."
            )
        gauge("aie_backup_size_bytes", st.get("size"), "Size of the last backup archive.")
        gauge("aie_backup_enabled", self.backups.enabled, "1 when nightly backups are on.")
        gauge("aie_disk_free_bytes", s.get("disk_free"), "Free bytes on the volume holding state_dir.")
        gauge("aie_disk_total_bytes", s.get("disk_total"), "Size of the volume holding state_dir.")
        gauge(
            "aie_db_size_bytes", s.get("db_size"), "Database size (SQLite files, or the PostgreSQL database)."
        )
        return "\n".join(out) + "\n"

    # the loop

    def _write_pid(self) -> None:
        try:
            self.pid_file.parent.mkdir(parents=True, exist_ok=True)
            self.pid_file.write_text(str(os.getpid()))
        except OSError:
            log.warning("ops: cannot write %s", self.pid_file)

    def _remove_pid(self) -> None:
        try:
            if self.pid_file.read_text().strip() == str(os.getpid()):
                self.pid_file.unlink()
        except OSError:
            pass

    async def tick(self) -> None:
        if self.backups.due(self.local_now()):
            with suppress(Exception):  # logged and recorded by run(); the watchdog reports it
                await self.backups.run()
        await self.watchdog.check()

    async def run(self, stopping: asyncio.Event) -> None:
        """Every `interval` seconds: the nightly backup when due, then the watchdog."""
        self._write_pid()
        try:
            while not stopping.is_set():
                try:
                    await self.tick()
                except Exception:
                    log.exception("ops: tick failed")
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=self.interval)
                except TimeoutError:
                    pass
        finally:
            self._remove_pid()
