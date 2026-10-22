# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Collectors: Redfish, LLDP (eAPI, NVUE, SNMP) and DCGM over asyncio + httpx (CONTRACT 3.4).

``HttpCollector`` speaks to real devices and to simhall alike (base URL + path). ``poll_all``
runs every device concurrently under one semaphore and never raises: failures become
``reachable=False`` observations or entries in ``Observations.lldp_errors``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from turnup_check.collectors.dcgm import parse_dcgm
from turnup_check.collectors.lldp_eapi import eapi_request_body, parse_eapi
from turnup_check.collectors.lldp_nvue import NVUE_INTERFACE_PARAMS, NVUE_INTERFACE_PATH, parse_nvue
from turnup_check.collectors.lldp_snmp import collect_lldp_snmp, host_from_url
from turnup_check.collectors.redfish import HttpFetcher, walk_redfish
from turnup_check.models import (
    Creds,
    DcgmObservation,
    Device,
    Intent,
    LldpNeighbor,
    Observations,
    RedfishObservation,
)

MAX_CONCURRENCY = 64
PER_HOST_CONCURRENCY = 4  # per device: BMCs handle few parallel requests; TURNUP_PER_HOST overrides
# Devices that share one host:port (simhall, a Redfish aggregator, a jump proxy) share one endpoint's
# capacity: at most this many requests start against it at once. A request keeps its shared slot only for
# SHARED_LEASE_S; one still waiting after that (a dead device, a slow body) gives the slot back, so dead
# devices never queue the live ones behind their timeouts (CONTRACT 9.4).
SHARED_ENDPOINT_CONCURRENCY = 4
SHARED_LEASE_S = 0.1
DEFAULT_TIMEOUT = 3.0
SOURCES: tuple[str, ...] = ("lldp", "redfish", "dcgm")


class CollectorError(Exception):
    """A switch could not be polled; recorded in ``Observations.lldp_errors``."""


@runtime_checkable
class Collector(Protocol):
    async def redfish(self, node: Device, creds: Creds, timeout: float) -> RedfishObservation: ...

    async def lldp(self, switch: Device, creds: Creds, timeout: float) -> list[LldpNeighbor]: ...

    async def dcgm(self, node: Device, timeout: float) -> DcgmObservation: ...


def device_key(url: str) -> str:
    """Limiter key for a device base URL: scheme-less ``host:port/path`` without a trailing slash."""
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path.rstrip('/')}"


def _tls_verify() -> bool:
    return os.environ.get("TURNUP_TLS_VERIFY", "").lower() in ("1", "true", "yes")


def _per_host_default() -> int:
    try:
        return max(1, int(os.environ.get("TURNUP_PER_HOST", PER_HOST_CONCURRENCY)))
    except ValueError:
        return PER_HOST_CONCURRENCY


class HttpCollector:
    """The production collector. Use as an async context manager or pass your own ``httpx.AsyncClient``."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        max_concurrency: int = MAX_CONCURRENCY,
        per_host: int | None = None,
        verify: bool | None = None,
    ) -> None:
        self._client = client
        self._own_client = client is None
        self._verify = _tls_verify() if verify is None else verify
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.per_host = _per_host_default() if per_host is None else max(1, per_host)
        self.shared_endpoint = max(self.per_host, SHARED_ENDPOINT_CONCURRENCY)
        self._host_sems: dict[str, asyncio.Semaphore] = {}
        self._endpoint_sems: dict[str, asyncio.Semaphore] = {}

    @asynccontextmanager
    async def limit(self, url: str) -> AsyncIterator[None]:
        """Bound requests per device (``per_host``), per shared endpoint and globally (``max_concurrency``).

        ``url`` is the device's base URL (``https://<ip>`` on real hardware, ``.../dev/<name>`` in simhall
        or behind a Redfish aggregator). The device limit is keyed on it, so devices that share one
        ``host:port`` do not queue behind each other's timeouts (CONTRACT 9.4); the shared ``host:port``
        slot is leased (``SHARED_LEASE_S``) so the endpoint is never flooded by live requests either.
        """
        key = device_key(url)
        sem = self._host_sems.get(key)
        if sem is None:
            sem = self._host_sems[key] = asyncio.Semaphore(self.per_host)
        netloc = urlsplit(url).netloc
        shared = self._endpoint_sems.get(netloc)
        if shared is None:
            shared = self._endpoint_sems[netloc] = asyncio.Semaphore(self.shared_endpoint)
        async with sem:
            await shared.acquire()
            released = False

            def give_back() -> None:
                nonlocal released
                if not released:
                    released = True
                    shared.release()

            try:
                async with self.semaphore:
                    lease = asyncio.get_running_loop().call_later(SHARED_LEASE_S, give_back)
                    try:
                        yield
                    finally:
                        lease.cancel()
            finally:
                give_back()

    async def __aenter__(self) -> HttpCollector:
        if self._client is None:
            limits = httpx.Limits(
                max_connections=MAX_CONCURRENCY + 8, max_keepalive_connections=MAX_CONCURRENCY
            )
            self._client = httpx.AsyncClient(verify=self._verify, limits=limits, follow_redirects=False)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("HttpCollector used outside 'async with'")
        return self._client

    # -- Redfish -------------------------------------------------------------------------

    async def redfish(
        self, node: Device, creds: Creds, timeout: float, *, want_ports: bool = False
    ) -> RedfishObservation:
        """Walk one BMC. ``want_ports`` adds the Chassis/NetworkAdapters/Ports branch (many requests)."""
        base = node.mgmt.bmc_url
        if not base:
            return RedfishObservation(reachable=False, raw_error="no bmc_url in intent")
        fetch = HttpFetcher(self.client, base, auth=creds.bmc(node.name), timeout=timeout, limiter=self.limit)
        return await walk_redfish(fetch, timeout=timeout, want_ports=want_ports)

    # -- LLDP ----------------------------------------------------------------------------

    async def lldp(self, switch: Device, creds: Creds, timeout: float) -> list[LldpNeighbor]:
        base = switch.mgmt.switch_url
        if not base:
            raise CollectorError("no switch_url in intent")
        proto = switch.lldp_protocol
        try:
            if proto == "eapi":
                return await self._lldp_eapi(switch, base, creds, timeout)
            if proto == "nvue":
                return await self._lldp_nvue(switch, base, creds, timeout)
            return await collect_lldp_snmp(
                switch.name, host_from_url(base), creds.community(switch.name), timeout
            )
        except CollectorError:
            raise
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise CollectorError(f"{proto}: timeout after {timeout:g} s") from exc
        except httpx.HTTPError as exc:
            raise CollectorError(f"{proto}: {exc.__class__.__name__}: {exc}") from exc
        except (OSError, ValueError, ImportError) as exc:
            raise CollectorError(f"{proto}: {exc}") from exc

    async def _lldp_eapi(self, switch: Device, base: str, creds: Creds, timeout: float) -> list[LldpNeighbor]:
        url = f"{base.rstrip('/')}/command-api"
        async with self.limit(base), asyncio.timeout(timeout):
            resp = await self.client.post(
                url,
                json=eapi_request_body(),
                auth=creds.switch(switch.name),
                timeout=timeout,
            )
        if resp.status_code >= 400:
            raise CollectorError(f"eapi: HTTP {resp.status_code}")
        doc = resp.json()
        if isinstance(doc, dict) and doc.get("error"):
            raise CollectorError(f"eapi: {doc['error'].get('message', doc['error'])}")
        result = doc.get("result") if isinstance(doc, dict) else None
        if not isinstance(result, list):
            raise CollectorError("eapi: response has no result list")
        return parse_eapi(result, switch.name)

    async def _lldp_nvue(self, switch: Device, base: str, creds: Creds, timeout: float) -> list[LldpNeighbor]:
        url = f"{base.rstrip('/')}{NVUE_INTERFACE_PATH}"
        async with self.limit(base), asyncio.timeout(timeout):
            resp = await self.client.get(
                url,
                params=NVUE_INTERFACE_PARAMS,
                auth=creds.switch(switch.name),
                timeout=timeout,
            )
        if resp.status_code >= 400:
            raise CollectorError(f"nvue: HTTP {resp.status_code}")
        doc = resp.json()
        if not isinstance(doc, dict):
            raise CollectorError("nvue: interface document is not an object")
        return parse_nvue(doc, switch.name)

    # -- DCGM ----------------------------------------------------------------------------

    async def dcgm(self, node: Device, timeout: float) -> DcgmObservation:
        base = node.mgmt.dcgm_url
        if not base:
            return DcgmObservation(reachable=False, raw_error="no dcgm_url in intent")
        url = f"{base.rstrip('/')}/metrics"
        try:
            # ``timeout`` is a deadline for the whole request (httpx's own is per read, so a device that
            # trickles its body would never trip it).
            async with self.limit(base), asyncio.timeout(timeout):
                resp = await self.client.get(url, timeout=timeout)
        except (httpx.TimeoutException, TimeoutError):
            return DcgmObservation(reachable=False, raw_error=f"timeout after {timeout:g} s")
        except httpx.HTTPError as exc:
            return DcgmObservation(reachable=False, raw_error=f"{exc.__class__.__name__}: {exc}")
        if resp.status_code >= 400:
            return DcgmObservation(reachable=False, raw_error=f"HTTP {resp.status_code}")
        return parse_dcgm(resp.text)


def normalize_only(only: Iterable[str] | str | None) -> list[str]:
    """``--only lldp,redfish`` -> ``["lldp", "redfish"]`` in canonical order; unknown names raise."""
    if only is None:
        return list(SOURCES)
    items = only.split(",") if isinstance(only, str) else list(only)
    wanted = {s.strip().lower() for s in items if s.strip()}
    unknown = wanted - set(SOURCES)
    if unknown:
        raise ValueError(f"unknown source(s) {', '.join(sorted(unknown))}; choose from {', '.join(SOURCES)}")
    return [s for s in SOURCES if s in wanted] or list(SOURCES)


async def _redfish_call(
    collector: Collector, node: Device, creds: Creds, timeout: float, want_ports: bool
) -> RedfishObservation:
    """Call ``collector.redfish`` with ``want_ports`` when the collector understands the keyword."""
    if not want_ports:
        return await collector.redfish(node, creds, timeout)
    try:
        return await collector.redfish(node, creds, timeout, want_ports=True)  # type: ignore[call-arg]
    except TypeError:
        return await collector.redfish(node, creds, timeout)


async def poll_all(
    intent: Intent,
    collector: Collector,
    creds: Creds | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    only: Iterable[str] | str | None = None,
    ports_for: Iterable[str] | None = None,
) -> Observations:
    """Poll every switch and node of the intent concurrently.

    NIC port details (Redfish Chassis/NetworkAdapters/Ports) are fetched only for the nodes named in
    ``ports_for``; ``poll_ports`` adds them later for the nodes the diff wants evidence on.
    """
    creds = creds or Creds()
    polled = normalize_only(only)
    want_ports = set(ports_for or ())
    obs = Observations(polled=polled)
    tasks: list[Any] = []

    async def do_lldp(sw: Device) -> None:
        obs.lldp_protocols[sw.name] = sw.lldp_protocol
        try:
            rows = await collector.lldp(sw, creds, timeout)
        except (CollectorError, ValueError) as exc:
            obs.lldp_errors[sw.name] = str(exc)
            return
        except Exception as exc:
            obs.lldp_errors[sw.name] = f"{exc.__class__.__name__}: {exc}"
            return
        if not rows:
            # A switch always has ports: an answer with none (wrong VRF, a proxy answering 200, an empty
            # revision) is unreadable, not "every cable unplugged" (CONTRACT C12).
            obs.lldp_errors[sw.name] = f"{sw.lldp_protocol}: empty interface table"
            return
        obs.lldp[sw.name] = rows

    async def do_redfish(node: Device) -> None:
        try:
            obs.redfish[node.name] = await _redfish_call(
                collector, node, creds, timeout, node.name in want_ports
            )
        except Exception as exc:
            obs.redfish[node.name] = RedfishObservation(
                reachable=False, raw_error=f"{exc.__class__.__name__}: {exc}"
            )

    async def do_dcgm(node: Device) -> None:
        try:
            obs.dcgm[node.name] = await collector.dcgm(node, timeout)
        except Exception as exc:
            obs.dcgm[node.name] = DcgmObservation(
                reachable=False, raw_error=f"{exc.__class__.__name__}: {exc}"
            )

    if "lldp" in polled:
        tasks.extend(do_lldp(sw) for sw in intent.switches)
    for node in intent.nodes:
        if "redfish" in polled:
            tasks.append(do_redfish(node))
        if "dcgm" in polled:
            tasks.append(do_dcgm(node))
    if tasks:
        await asyncio.gather(*tasks)
    return obs


async def poll_ports(
    intent: Intent,
    collector: Collector,
    creds: Creds | None,
    *,
    timeout: float,
    nodes: Iterable[str],
    obs: Observations,
) -> list[str]:
    """Second pass: fetch NIC port details for ``nodes`` and merge them into ``obs``.

    Returns the names of the nodes whose observation was replaced.
    """
    creds = creds or Creds()
    names = set(nodes)
    wanted = [n for n in intent.nodes if n.name in names]
    updated: list[str] = []

    async def one(node: Device) -> None:
        try:
            fresh = await _redfish_call(collector, node, creds, timeout, True)
        except Exception:
            return  # keep the first-pass observation
        current = obs.redfish.get(node.name)
        if current is not None and current.reachable and not fresh.reachable:
            return  # a transient failure must not turn a reachable node into a finding
        obs.redfish[node.name] = fresh
        updated.append(node.name)

    if wanted:
        await asyncio.gather(*(one(n) for n in wanted))
    return updated


__all__ = [
    "DEFAULT_TIMEOUT",
    "MAX_CONCURRENCY",
    "PER_HOST_CONCURRENCY",
    "SHARED_ENDPOINT_CONCURRENCY",
    "SHARED_LEASE_S",
    "SOURCES",
    "Collector",
    "CollectorError",
    "HttpCollector",
    "device_key",
    "normalize_only",
    "poll_all",
    "poll_ports",
]
