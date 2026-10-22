# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Helpers for the integration tier against simhall (CONTRACT sections 6, 8, 9).

Nothing here binds a fixed port: every server gets a free 127.0.0.1 port. When ``simhall serve``
does not come up the caller skips, it never fails.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

STARTUP_TIMEOUT_S = 20.0
CHECK, CROSS, DOT, ARROW = chr(0x2713), chr(0x2717), chr(0xB7), chr(0x2192)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class SimServer:
    """A ``python -m simhall serve`` subprocess plus a thin control-plane client."""

    def __init__(self, base_url: str, proc: subprocess.Popen[bytes], log_path: Path) -> None:
        self.base_url = base_url
        self.proc = proc
        self.log_path = log_path
        self.client = httpx.Client(base_url=base_url, timeout=10.0)

    @property
    def netbox_url(self) -> str:
        return f"{self.base_url}/netbox"

    def intent(self) -> dict[str, Any]:
        r = self.client.get("/sim/intent")
        r.raise_for_status()
        return r.json()

    def faults(self) -> list[dict[str, Any]]:
        r = self.client.get("/sim/faults")
        r.raise_for_status()
        doc = r.json()
        return doc if isinstance(doc, list) else doc.get("faults", [])

    def inject(self, kind: str, **params: Any) -> dict[str, Any]:
        """POST /sim/faults; tries the flat shape first, then ``{"kind", "params"}``."""
        r = self.client.post("/sim/faults", json={"kind": kind, **params})
        if r.status_code >= 400:
            r = self.client.post("/sim/faults", json={"kind": kind, "params": params})
        r.raise_for_status()
        return r.json() if r.content else {}

    def clear(self, fault_id: str) -> None:
        self.client.delete(f"/sim/faults/{fault_id}").raise_for_status()

    def act(self, kind: str, **params: Any) -> None:
        r = self.client.post("/sim/actions", json={"kind": kind, **params})
        if r.status_code >= 400:
            r = self.client.post("/sim/actions", json={"kind": kind, "params": params})
        r.raise_for_status()

    def reset(self) -> None:
        self.client.post("/sim/reset").raise_for_status()

    def log(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            return ""

    def stop(self) -> None:
        self.client.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


def start_server(args: list[str], workdir: Path) -> SimServer:
    """Start ``python -m simhall serve <args> --port <free>`` and wait for ``/sim/intent``.

    Skips the calling test when the server never answers.
    """
    port = free_port()
    log_path = workdir / f"simhall-{port}.log"
    cmd = [sys.executable, "-m", "simhall", "serve", *args, "--port", str(port)]
    with log_path.open("wb") as log:
        proc = subprocess.Popen(cmd, cwd=workdir, stdout=log, stderr=subprocess.STDOUT, env=os.environ.copy())
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    last_error = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            r = httpx.get(f"{base}/sim/intent", timeout=1.0)
            if r.status_code == 200:
                return SimServer(base, proc, log_path)
            last_error = f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            last_error = exc.__class__.__name__
        time.sleep(0.2)
    tail = ""
    with contextlib.suppress(OSError):
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
    if proc.poll() is None:
        proc.kill()
    pytest.skip(f"simhall serve did not come up on {base} ({last_error}); log tail: {tail!r}")


def run_cli(args: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "turnup_check.cli", *args]
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=120, check=False)


def cli_env(tmp_path: Path) -> dict[str, str]:
    """Environment for the CLI subprocess: isolated config dir, UTF-8 output, no counter URL override.

    No ``NETBOX_TOKEN``: simhall's NetBox API accepts requests without one, like the README's command.
    ``TURNUP_PER_HOST`` is left at its default (4): simhall collapses every device into one host:port, so
    the per-host limit serialises requests that would run in parallel against real BMCs; chaos tests size
    their fault sets with that in mind (two ``timeout`` nodes cost one timeout, not two).
    """
    env = os.environ.copy()
    env.pop("NETBOX_TOKEN", None)
    env["TURNUP_CONFIG_DIR"] = str(tmp_path / "cfg")
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("TURNUP_COUNTER_URL", None)
    env.pop("TURNUP_PER_HOST", None)
    return env


# --------------------------------------------------------------------------------------------------
# In-process: the production collector and engine over simhall's ASGI app, no sockets
# --------------------------------------------------------------------------------------------------


class TimeoutASGITransport(httpx.ASGITransport):
    """``httpx.ASGITransport`` that honours the request's read timeout the way a socket would.

    httpx does not enforce timeouts on ASGI transports; a simhall device in ``timeout`` mode would stall
    the test for ``--timeout-sleep`` seconds instead of surfacing as a ``ReadTimeout`` after the
    collector's deadline. This wrapper cancels the in-flight ASGI call at the deadline.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        timeout = (request.extensions.get("timeout") or {}).get("read")
        try:
            return await asyncio.wait_for(super().handle_async_request(request), timeout)
        except TimeoutError as exc:
            raise httpx.ReadTimeout(f"timed out after {timeout} s", request=request) from exc


IN_PROCESS_BASE_URL = "http://127.0.0.1:8900"  # any host works: the ASGI transport routes it to the app


async def check_in_process(
    hall: Any, *, timeout: float = 0.3, only: str | None = None
) -> tuple[Any, Any, Any]:
    """Run ``turnup_check.engine.check`` against a simhall ``Hall`` through its real FastAPI app.

    Requests go through ``HttpCollector`` (Redfish walk, NVUE/eAPI, DCGM, both passes) exactly as over a
    socket, minus the socket. ``per_host`` is raised to the global limit because every simulated device
    shares one host:port; in a real hall each BMC is its own host.
    Returns ``(report, diff_result, observations)``.
    """
    from simhall.server import create_app  # type: ignore[import-not-found]

    from turnup_check.collectors import HttpCollector
    from turnup_check.engine import check
    from turnup_check.models import Creds, Intent
    from turnup_check.report import build_report

    hall.base_url = IN_PROCESS_BASE_URL
    app = create_app(hall, timeout_sleep_s=max(5.0, 20 * timeout))
    intent = Intent.model_validate(hall.intent())
    transport = TimeoutASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url=IN_PROCESS_BASE_URL) as client,
        HttpCollector(client=client, per_host=64) as collector,
    ):
        started = time.monotonic()
        obs, result = await check(intent, collector, Creds(), timeout=timeout, only=only)
        elapsed = time.monotonic() - started
    report = build_report(intent, obs, result, elapsed_s=elapsed)
    return report, result, obs


def run_check(hall: Any, **kw: Any) -> tuple[Any, Any, Any]:
    """Synchronous ``check_in_process`` (one fresh event loop per call; hypothesis-friendly)."""
    return asyncio.run(check_in_process(hall, **kw))


def make_fault(kind: str, **params: Any) -> Any:
    """Build a ``simhall.Fault`` whatever its constructor shape: kwargs, ``params=`` or positional dict."""
    from simhall import Fault  # type: ignore[import-not-found]

    try:
        return Fault(kind=kind, **params)
    except TypeError:
        pass
    try:
        return Fault(kind=kind, params=params)
    except TypeError:
        return Fault(kind, params)
