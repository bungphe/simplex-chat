"""CLI: `python -m ai_employees run employees.yaml` (or `check` to validate),
`backup employees.yaml` to write a backup now, `restore <file> --config employees.yaml --yes`
to put one back (stop the office first; see docs/OPERATIONS.md)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from . import skills as sk
from .config import ConfigError, load_config
from .employee import Office, prepare_skills


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ai_employees", description="AI employees on SimpleX Chat")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")
    for name, help_ in (
        ("run", "run the office"),
        ("check", "validate the config and show what it declares"),
        ("backup", "write a backup archive now (the office may keep running)"),
    ):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("config", help="path to employees.yaml")
        sp.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    rs = sub.add_parser("restore", help="restore a backup archive (stop the office first)")
    rs.add_argument("file", help="backup-YYYYmmdd-HHMMSS.tar.gz")
    rs.add_argument("--config", required=True, help="path to employees.yaml")
    rs.add_argument("--yes", action="store_true", help="confirm: the current state is moved aside")
    rs.add_argument("--extras", action="store_true", help="also restore the config and knowledge files")
    rs.add_argument("--force", action="store_true", help="restore even if office.pid says the office runs")
    rs.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    return p


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "saas":  # the SaaS control plane has its own options (saas/)
        from .saas import main as saas_main

        saas_main(sys.argv[2:])
        return
    args = _parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # request logs show full URLs, and Telegram's carry the bot token
    for noisy in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        config = load_config(args.config)
        if args.command != "restore":
            prepare_skills(config)
    except (ConfigError, KeyError, ImportError, ValueError) as e:
        sys.exit(f"config error: {e}")

    if args.command == "check":
        for name, m in config.models.items():
            key = "key set" if m.key() else (f"{m.api_key_env} NOT SET" if m.api_key_env else "no key")
            print(f"model {name}: {m.describe()} ({key})")
        for e in config.employees:
            profile = config.model_profile(e.model)
            print(
                f"{e.id}: {e.display_name} | model={e.model} ({profile.describe() if profile else '?'}) "
                f"| skills: {', '.join(sk.expand(e.skills))}"
            )
            for r in e.routines:
                print(f"  routine {r.id}: {r.describe()} -> {r.deliver}")
        for name, a in config.actions.items():
            target = "inventory order" if a.kind == "stock_order" else f"{a.method} {a.url}"
            print(f"action {name}: {target} (fields: {', '.join(a.fields)})")
        if config.admin_ui:
            ui = config.admin_ui
            pw = (
                "password set"
                if ui.password
                else "NO PASSWORD: set password_env, or `run` will refuse to start"
            )
            print(f"admin UI: http://{ui.host}:{ui.port} ({pw})")
        print(f"available skills: {', '.join(sk.available())}")
        return

    if args.command == "backup":
        from .db import Database, DocStore, connect
        from .ops import Backups

        # the status goes where the running office reads it (its docs store)
        db = (
            connect(config.database_url)
            if config.database_url
            else Database(str(Path(config.state_dir) / "office.sqlite"))
        )
        backups = Backups(config, DocStore(db), extras=[args.config])
        try:
            path = asyncio.run(backups.run())
        except Exception as e:  # noqa: BLE001 - reported on the console, exit code 1
            sys.exit(f"backup failed: {e}")
        print(f"{path} ({path.stat().st_size / 1e6:.1f} MB)")
        return

    if args.command == "restore":
        from .ops import Backups, running_pid

        if not args.yes:
            sys.exit("restore replaces the office's data: stop the office, then add --yes")
        if (pid := running_pid(config.state_dir)) and not args.force:
            sys.exit(f"the office seems to be running (pid {pid} in state_dir/office.pid): stop it first")
        archive = Path(args.file)
        if not archive.is_file():
            sys.exit(f"no such file: {archive}")
        try:
            notes = Backups(config).restore(archive, extras=args.extras)
        except Exception as e:  # noqa: BLE001 - reported on the console, exit code 1
            sys.exit(f"restore failed: {e}")
        for note in notes:
            print(note)
        print(f"restored {archive} into {config.state_dir}")
        return

    if config.admin_ui and not config.admin_ui.password:
        sys.exit("config error: admin_ui needs a password (set the variable named in password_env)")
    office = Office(config)
    office.ops.backups.extras.append(Path(args.config))  # the config file goes into every backup

    async def run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, office.stop)
        await office.run()

    asyncio.run(run())


if __name__ == "__main__":
    main()
