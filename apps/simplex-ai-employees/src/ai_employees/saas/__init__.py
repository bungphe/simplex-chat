"""The SaaS control plane: `python -m ai_employees saas --config saas.yaml`.

A separate aiohttp service (127.0.0.1:8090 by default, behind Caddy or nginx with TLS)
with its own database. It signs shops up, provisions one product deployment per tenant
(provisioner.py), bills them monthly (billing.py) and serves a public site, a tenant
portal and an operator console (web.py). See docs/SAAS.md.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta

from aiohttp import web as aioweb  # `web` is this package's own submodule

from ..config import ConfigError
from ..db import connect
from .billing import Billing
from .config import SaasConfig, load_saas_config
from .notify import Mailer, Notifier
from .provisioner import make_backend
from .service import Service
from .store import SaasStore
from .web import create_app

log = logging.getLogger(__name__)
DAILY_AT = 3  # o'clock, local time


def build(cfg: SaasConfig) -> tuple[Service, Billing]:
    store = SaasStore(connect(cfg.database_url))
    service = Service(cfg, store, make_backend(cfg), Notifier(cfg, Mailer(cfg.smtp)))
    return service, Billing(service)


async def daily_loop(billing: Billing) -> None:
    while True:
        now = datetime.now().astimezone()
        at = now.replace(hour=DAILY_AT, minute=0, second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        await asyncio.sleep((at - now).total_seconds())
        try:
            counts = await billing.daily()
            log.info("daily job: %s", counts)
        except Exception:
            log.exception("daily job failed")


async def serve(cfg: SaasConfig) -> None:
    service, billing = build(cfg)
    app = create_app(cfg, service, billing)
    runner = aioweb.AppRunner(app, access_log=None)
    await runner.setup()
    await aioweb.TCPSite(runner, cfg.host, cfg.port).start()
    log.info(
        "saas control plane on http://%s:%d (%s, %d plans)",
        cfg.host,
        cfg.port,
        cfg.public_url,
        len(cfg.plans),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    job = asyncio.create_task(daily_loop(billing))
    await stop.wait()
    job.cancel()
    await service.drain()
    await runner.cleanup()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="ai_employees saas", description="SaaS control plane for AI employees")
    p.add_argument("--config", required=True, help="path to saas.yaml")
    p.add_argument("--daily", action="store_true", help="run the daily billing job once and exit (for cron)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = load_saas_config(args.config)
    except ConfigError as e:
        sys.exit(f"config error: {e}")
    if args.daily:
        _service, billing = build(cfg)
        print(asyncio.run(billing.daily()))
        return
    asyncio.run(serve(cfg))
