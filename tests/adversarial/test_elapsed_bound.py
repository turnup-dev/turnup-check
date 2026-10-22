# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""CONTRACT 9.4: with timeouts, HTTP 500s and dead exporters "elapsed stays bounded by timeout x 2".

Two ways the bound breaks, both over real HTTP:

1. The per-host limiter (4 requests per `host:port`) also bounds requests that are waiting out a
   timeout. In simhall every device sits behind one `host:port` (as BMCs do behind a Redfish
   aggregator or jump proxy), so N timing-out nodes cost about N x 2 / 4 timeouts: 10 dead nodes at
   `--timeout 1` take ~5 s. (A 180-node hall with 44 dead nodes measured 24 s.)
2. `--timeout` is httpx's per-phase timeout, not a deadline. A device that trickles its response a few
   bytes every 0.3 s never trips it; nothing bounds the DCGM fetch or the first three Redfish fetches,
   so one misbehaving exporter stalls the run for as long as it keeps trickling.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import httpx
import pytest
from adv_helpers import Hall, Intent, check, small_spec

from turnup_check.collectors import HttpCollector, poll_all
from turnup_check.models import Creds

pytestmark = pytest.mark.integration

TIMEOUT = 1.0
ALLOWANCE = 1.0  # same allowance as tests/integration/test_chaos.py


@pytest.mark.timeout(60)
def test_many_timeouts_behind_one_host_stay_within_two_timeouts():
    from simhall.server import ServerThread, create_app

    srv = ServerThread(create_app(Hall.generate(small_spec(nodes_per_rack=12)), timeout_sleep_s=3.0)).start()
    try:
        with httpx.Client(base_url=srv.base_url, timeout=10.0) as ctl:
            for n in range(1, 11):
                fault = {"kind": "unreachable_node", "node": f"node{n}", "mode": "timeout"}
                r = ctl.post("/sim/faults", json=fault)
                assert r.status_code < 300, r.text
            intent = Intent.model_validate(ctl.get("/sim/intent").json())

        async def run() -> float:
            async with HttpCollector() as col:
                t0 = time.monotonic()
                _, res = await check(intent, col, Creds(), timeout=TIMEOUT)
                assert sorted(f.device for f in res.findings if f.class_ == "unreachable_node") == sorted(
                    f"node{n}" for n in range(1, 11)
                )
                return time.monotonic() - t0

        elapsed = asyncio.run(run())
    finally:
        srv.stop()
    assert elapsed <= 2 * TIMEOUT + ALLOWANCE, elapsed


def _trickle_server(total_s: float) -> tuple[socket.socket, str]:
    """HTTP server answering 200 with a body sent 4 bytes every 0.3 s for about ``total_s``."""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)

    def handle(conn: socket.socket) -> None:
        try:
            conn.recv(65536)
            body = b"# x\n" * int(total_s / 0.3)
            head = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n\r\n"
            conn.sendall(head % len(body))
            for i in range(0, len(body), 4):
                conn.sendall(body[i : i + 4])
                time.sleep(0.3)
        except OSError:
            pass
        finally:
            conn.close()

    def serve() -> None:
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return sock, f"http://127.0.0.1:{sock.getsockname()[1]}"


@pytest.mark.timeout(60)
def test_trickling_device_is_cut_off_at_the_deadline():
    sock, base = _trickle_server(6.0)
    intent = Intent.model_validate(
        {
            "schema": "turnup.intent/v1",
            "site": "t",
            "devices": [{"name": "node1", "role": "gpu-node", "model": "HGX", "mgmt": {"dcgm_url": base}}],
        }
    )

    async def run() -> float:
        async with HttpCollector() as col:
            t0 = time.monotonic()
            await poll_all(intent, col, Creds(), timeout=TIMEOUT, only="dcgm")
            return time.monotonic() - t0

    try:
        elapsed = asyncio.run(run())
    finally:
        sock.close()
    assert elapsed <= 2 * TIMEOUT + ALLOWANCE, elapsed
