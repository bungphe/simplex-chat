"""CLI: `python -m ai_employees run employees.yaml` (or `check` to validate)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from . import skills as sk
from .config import ConfigError, load_config
from .employee import Office, prepare_skills


def main() -> None:
    p = argparse.ArgumentParser(prog="ai_employees", description="AI employees on SimpleX Chat")
    p.add_argument("command", choices=["run", "check"])
    p.add_argument("config", help="path to employees.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # request logs show full URLs, and Telegram's carry the bot token
    for noisy in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        config = load_config(args.config)
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
            print(f"action {name}: {a.method} {a.url} (fields: {', '.join(a.fields)})")
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

    if config.admin_ui and not config.admin_ui.password:
        sys.exit("config error: admin_ui needs a password (set the variable named in password_env)")
    office = Office(config)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, office.stop)
        await office.run()

    asyncio.run(run())


if __name__ == "__main__":
    main()
