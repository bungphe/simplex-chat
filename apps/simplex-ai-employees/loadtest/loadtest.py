"""Load test: how many customer messages per second can one office process answer?

    python loadtest/loadtest.py --rates 50,100,200,400 --seconds 20 --conversations 5000

Starts a fake OpenAI-compatible model (fixed latency), a collector for replies, and the
office in its own process (web API + channel hub, no SimpleX). Customer messages are sent
to a webhook channel at a fixed rate over many conversations (open loop), and each reply
is matched to its message. Reports throughput, end-to-end latency and the office's CPU.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

HERE = Path(__file__).resolve().parent
LLM_PORT, SINK_PORT, OFFICE_PORT = 18101, 18102, 18103
SECRET = "loadtest-secret-0123456789"


class Stats:
    def __init__(self) -> None:
        self.sent: dict[str, float] = {}  # conversation id -> time of its last unanswered message
        self.latencies: list[float] = []
        self.hook_ms: list[float] = []
        self.errors = 0
        self.replies = 0
        self.llm_calls = 0


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    values = sorted(values)
    return values[min(len(values) - 1, int(p / 100 * len(values)))]


async def start_fakes(stats: Stats, llm_latency: float) -> web.AppRunner:
    async def completions(request: web.Request) -> web.Response:
        stats.llm_calls += 1
        await asyncio.sleep(llm_latency * random.uniform(0.7, 1.3))
        body = {"choices": [{"message": {"role": "assistant", "content": "Dạ, máy MA-100 giá 4.500.000đ ạ."}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3000, "completion_tokens": 40}}
        return web.json_response(body)

    async def reply(request: web.Request) -> web.Response:
        data = await request.json()
        started = stats.sent.pop(data["conversation_id"], None)
        if started is not None:
            stats.latencies.append(time.monotonic() - started)
        stats.replies += 1
        return web.json_response({"message_id": f"r{stats.replies}"})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", completions)
    app.router.add_post("/reply", reply)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", LLM_PORT).start()
    await web.TCPSite(runner, "127.0.0.1", SINK_PORT).start()
    return runner


def write_config(state_dir: str, extra: dict) -> str:
    import yaml

    cfg = {
        "state_dir": state_dir,
        "models": {"fake": {"provider": "openai", "base_url": f"http://127.0.0.1:{LLM_PORT}/v1", "model": "fake"}},
        "defaults": {"model": "fake", "history_messages": 20},
        "admin_ui": {"host": "127.0.0.1", "port": OFFICE_PORT, "password": "loadtest-password"},
        "channels": [{"id": "website", "type": "webhook", "employee": "sales", "secret": SECRET,
                      "reply_url": f"http://127.0.0.1:{SINK_PORT}/reply", "debounce_seconds": 0.2}],
        "employees": [{"id": "sales", "display_name": "Lan", "db": f"{state_dir}/db-sales",
                       "system_prompt": "Bạn là Lan, nhân viên bán hàng. " * 40,
                       "skills": ["memory", "current_time"]}],
        **extra,
    }
    path = Path(state_dir) / "office.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True))
    return str(path)


def cpu_seconds(pid: int) -> float:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def rss_mb(pid: int) -> float:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


async def run_rate(session: ClientSession, stats: Stats, rate: float, seconds: float, conversations: int, pid: int) -> dict:
    stats.latencies.clear(); stats.hook_ms.clear(); stats.errors = 0
    replies0, calls0, cpu0 = stats.replies, stats.llm_calls, cpu_seconds(pid)
    sent = 0
    start = time.monotonic()
    tasks: set[asyncio.Task] = set()

    async def send(i: int) -> None:
        conv = f"lt-{i % conversations}"
        body = {"conversation_id": conv, "customer_name": f"Khách {i % conversations}",
                "text": f"Cho mình hỏi giá máy lọc nước MA-100, lần {i}", "message_id": f"m{i}-{random.random()}"}
        t0 = time.monotonic()
        stats.sent.setdefault(conv, t0)
        try:
            async with session.post(f"http://127.0.0.1:{OFFICE_PORT}/hooks/website", json=body,
                                    headers={"X-Hook-Secret": SECRET}) as r:
                await r.read()
                if r.status != 200:
                    stats.errors += 1
        except Exception:  # noqa: BLE001 - timeouts and refused connections count as errors
            stats.errors += 1
        stats.hook_ms.append((time.monotonic() - t0) * 1000)

    i = 0
    while (elapsed := time.monotonic() - start) < seconds:
        due = int(elapsed * rate)
        while sent < due:
            t = asyncio.create_task(send(random.randrange(10**9)))
            tasks.add(t); t.add_done_callback(tasks.discard)
            sent += 1; i += 1
        await asyncio.sleep(0.005)
    await asyncio.gather(*tasks, return_exceptions=True)
    # let replies drain (up to 30 s)
    drain_until = time.monotonic() + 30
    while stats.sent and time.monotonic() < drain_until:
        await asyncio.sleep(0.2)
    wall = time.monotonic() - start
    unanswered = len(stats.sent)
    stats.sent.clear()
    return {
        "rate": rate, "sent": sent, "errors": stats.errors,
        "replies": stats.replies - replies0, "llm_calls": stats.llm_calls - calls0, "unanswered": unanswered,
        "hook_p50": pct(stats.hook_ms, 50), "hook_p99": pct(stats.hook_ms, 99),
        "e2e_p50": pct(stats.latencies, 50), "e2e_p95": pct(stats.latencies, 95), "e2e_p99": pct(stats.latencies, 99),
        "cpu": (cpu_seconds(pid) - cpu0) / wall, "rss": rss_mb(pid),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rates", default="25,50,100,200")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--conversations", type=int, default=5000)
    ap.add_argument("--llm-latency", type=float, default=1.0)
    ap.add_argument("--config", default="{}", help="extra top-level config as JSON (e.g. database_url)")
    ap.add_argument("--json", help="write results here")
    ap.add_argument("--profile", help="record a py-spy profile of the office to this file (SVG)")
    args = ap.parse_args()

    stats = Stats()
    fakes = await start_fakes(stats, args.llm_latency)
    state_dir = tempfile.mkdtemp(prefix="aie-load-")
    cfg = write_config(state_dir, json.loads(args.config))
    env = {**os.environ, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    proc = subprocess.Popen([sys.executable, str(HERE / "office_runner.py"), cfg], stdout=subprocess.PIPE, env=env)
    assert proc.stdout is not None
    while b"READY" not in proc.stdout.readline():
        if proc.poll() is not None:
            raise SystemExit("office failed to start")
    spy = None
    if args.profile:
        spy = subprocess.Popen(["py-spy", "record", "-p", str(proc.pid), "-o", args.profile, "-f", "raw", "--nonblocking"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results = []
    try:
        async with ClientSession(connector=TCPConnector(limit=2000), timeout=ClientTimeout(total=30)) as session:
            for rate in [float(r) for r in args.rates.split(",")]:
                r = await run_rate(session, stats, rate, args.seconds, args.conversations, proc.pid)
                results.append(r)
                print(
                    f"{r['rate']:>6.0f}/s  sent {r['sent']:>6}  err {r['errors']:>4}  replies {r['replies']:>6}  "
                    f"unanswered {r['unanswered']:>5}  hook p50/p99 {r['hook_p50']:.0f}/{r['hook_p99']:.0f} ms  "
                    f"reply p50/p95/p99 {r['e2e_p50']:.2f}/{r['e2e_p95']:.2f}/{r['e2e_p99']:.2f} s  "
                    f"cpu {r['cpu']*100:.0f}%  rss {r['rss']:.0f} MB",
                    flush=True,
                )
    finally:
        if spy is not None:
            spy.send_signal(2); spy.wait(timeout=30)
        proc.terminate(); proc.wait(timeout=10)
        await fakes.cleanup()
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
