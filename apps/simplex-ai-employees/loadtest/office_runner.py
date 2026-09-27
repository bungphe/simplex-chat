"""Run an office's web API and channel hub without SimpleX accounts, for load tests."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import yaml

from ai_employees.config import parse_config
from ai_employees.employee import Office
from ai_employees.web import start_admin_ui


async def main(config_path: str) -> None:
    raw = yaml.safe_load(Path(config_path).read_text())
    if port := os.environ.get("AIE_PORT"):  # several processes from one config
        raw["admin_ui"]["port"] = int(port)
    config = parse_config(raw, base_dir=Path(config_path).parent)
    office = Office(config)
    runner = await start_admin_ui(office, config.admin_ui)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    print("READY", flush=True)
    tasks = [asyncio.create_task(office.hub.run(stopping)), asyncio.create_task(office.cluster.run(stopping))]
    await stopping.wait()
    for t in tasks:
        t.cancel()
    await runner.cleanup()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main(sys.argv[1]))
