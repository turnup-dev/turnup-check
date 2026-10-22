# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Redfish collector for nodes (DMTF Redfish, public schema), per CONTRACT.md section 3.1.

The walk is written against an abstract ``fetch(path) -> dict`` so the same code runs over
HTTP (``HttpFetcher``) and over an in-memory resource tree (``DictFetcher``, used by tests and by
the simhall property tier). Links are ``@odata.id`` paths, host-absolute (``/redfish/v1/...``),
resolved against the device prefix, never against the host root.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from turnup_check.models import PortObservation, RedfishObservation, normalize_mac

ROOT = "/redfish/v1/"

# Component match on FirmwareInventory ``Name`` (case-insensitive, CONTRACT 3.1). Short tokens match on
# word boundaries so "VBIOS" is not "BIOS" and "BlueField" is not "UEFI"; ``gpu_vbios`` is tested first,
# ``nic`` before ``bios``. "Remote Access Controller" (iDRAC's inventory name) is an addition to the list.
COMPONENT_TOKENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gpu_vbios", ("VBIOS", "GPU")),
    ("bmc", ("BMC", "iDRAC", "iLO", "XCC", "Baseboard Management", "Remote Access Controller")),
    ("nic", ("ConnectX", "BlueField", "NIC", "Network Adapter")),
    ("bios", ("BIOS", "UEFI")),
)
_COMPONENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (
        component,
        re.compile(
            "|".join(
                rf"\b{re.escape(t)}\b" if " " not in t and len(t) <= 5 else re.escape(t) for t in tokens
            ),
            re.IGNORECASE,
        ),
    )
    for component, tokens in COMPONENT_TOKENS
)
_SKIP_INVENTORY = re.compile(r"diagnostic|driver pack|previous", re.IGNORECASE)
# Inactive images: the backup/standby BMC bank, staged or pending updates, recovery/golden copies.
_INACTIVE_IMAGE = re.compile(
    r"\b(backup|standby|inactive|alternate|secondary|pending|staged|recovery|golden|rollback)\b",
    re.IGNORECASE,
)
COMPONENTS: tuple[str, ...] = tuple(c for c, _ in COMPONENT_TOKENS)

FetchKind = Literal["transport", "http", "decode"]


class FetchError(Exception):
    """A resource could not be fetched. ``transport`` errors stop the walk; ``http`` errors skip a branch."""

    def __init__(self, kind: FetchKind, detail: str, status: int | None = None) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.status = status

    def __str__(self) -> str:
        if self.status is not None:
            return f"HTTP {self.status}"
        return self.detail


Fetcher = Callable[[str], Awaitable[dict[str, Any]]]


def component_for(name: str | None) -> str | None:
    """Map a FirmwareInventory member name to a baseline component key, or ``None``."""
    if not name or _SKIP_INVENTORY.search(name):
        return None
    for component, pattern in _COMPONENT_PATTERNS:
        if pattern.search(name):
            return component
    return None


def odata_path(link: Any) -> str | None:
    """``{"@odata.id": "/redfish/v1/Systems"}`` or a string -> ``/redfish/v1/Systems``.

    Full URLs keep only their path so every link is resolved against the device prefix.
    """
    if isinstance(link, Mapping):
        link = link.get("@odata.id")
    if not isinstance(link, str) or not link:
        return None
    if "://" in link:
        return urlsplit(link).path or None
    return link if link.startswith("/") else "/" + link


def _members(coll: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for m in coll.get("Members") or []:
        p = odata_path(m)
        if p:
            out.append(p)
    return out


class DictFetcher:
    """Fetcher over a ``path -> resource`` mapping (a recorded tree or ``hall.redfish_tree(node)``)."""

    def __init__(
        self,
        tree: Mapping[str, Any],
        *,
        unreachable: bool = False,
        status_overrides: Mapping[str, int] | None = None,
    ):
        self.tree = {self._norm(k): v for k, v in tree.items()}
        self.unreachable = unreachable
        self.status_overrides = {self._norm(k): v for k, v in (status_overrides or {}).items()}
        self.calls: list[str] = []

    @staticmethod
    def _norm(path: str) -> str:
        p = path if path.startswith("/") else "/" + path
        return p.rstrip("/") or "/"

    async def __call__(self, path: str) -> dict[str, Any]:
        self.calls.append(path)
        if self.unreachable:
            raise FetchError("transport", "connection refused")
        key = self._norm(path)
        if key in self.status_overrides:
            raise FetchError("http", f"HTTP {self.status_overrides[key]}", status=self.status_overrides[key])
        if key not in self.tree:
            raise FetchError("http", "HTTP 404", status=404)
        value = self.tree[key]
        if not isinstance(value, Mapping):
            raise FetchError("decode", "resource is not a JSON object")
        return dict(value)


Limiter = Callable[[str], AbstractAsyncContextManager[None]]


@asynccontextmanager
async def _unlimited(_url: str) -> AsyncIterator[None]:
    yield


class HttpFetcher:
    """Fetcher over httpx with basic auth, a per-request timeout and an optional concurrency limiter."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        *,
        auth: tuple[str, str] | None,
        timeout: float,
        limiter: Limiter | None = None,
    ) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.timeout = timeout
        self.limiter: Limiter = limiter or _unlimited

    async def __call__(self, path: str) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            # The limiter is per device (keyed on the base URL); ``timeout`` bounds the whole request,
            # body included (httpx's own timeout is per read, which a trickling BMC never trips).
            async with self.limiter(self.base_url), asyncio.timeout(self.timeout):
                resp = await self.client.get(url, auth=self.auth, timeout=self.timeout)
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise FetchError("transport", f"timeout after {self.timeout:g} s") from exc
        except httpx.HTTPError as exc:
            raise FetchError("transport", f"{exc.__class__.__name__}: {exc}") from exc
        if resp.status_code >= 400:
            raise FetchError("http", f"HTTP {resp.status_code}", status=resp.status_code)
        try:
            data = resp.json()
        except ValueError as exc:
            raise FetchError("decode", "response is not JSON") from exc
        if not isinstance(data, dict):
            raise FetchError("decode", "resource is not a JSON object")
        return data


async def _try_fetch(fetch: Fetcher, path: str | None) -> dict[str, Any] | None:
    """Fetch an optional resource; HTTP/decoding errors give ``None``; transport errors propagate."""
    if not path:
        return None
    try:
        return await fetch(path)
    except FetchError as exc:
        if exc.kind == "transport":
            raise
        return None


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def parse_port(res: Mapping[str, Any]) -> PortObservation:
    """One ``NetworkAdapters/<id>/Ports/<id>`` (or legacy ``NetworkPorts``, or ``EthernetInterface``)
    resource -> PortObservation."""
    eth = res.get("Ethernet") or {}
    lldp = eth.get("LLDPReceive") or {}
    mac = (
        _first(eth.get("AssociatedMACAddresses"))
        or _first(res.get("AssociatedNetworkAddresses"))
        or res.get("MACAddress")
        or res.get("PermanentMACAddress")
    )
    speed: float | None = None
    if isinstance(res.get("CurrentSpeedGbps"), int | float):
        speed = float(res["CurrentSpeedGbps"])
    elif isinstance(res.get("CurrentLinkSpeedMbps"), int | float):
        speed = float(res["CurrentLinkSpeedMbps"]) / 1000.0
    elif isinstance(res.get("SpeedMbps"), int | float):
        speed = float(res["SpeedMbps"]) / 1000.0
    status = res.get("LinkStatus")
    if status == "Up":
        status = "LinkUp"
    elif status == "Down":
        status = "LinkDown"
    return PortObservation(
        id=str(res.get("Id") or ""),
        name=res.get("Name"),
        mac=normalize_mac(mac) if isinstance(mac, str) else None,
        link_status=status if isinstance(status, str) else None,
        speed_gbps=speed,
        lldp_system=lldp.get("SystemName") or None,
        lldp_port=lldp.get("PortId") or None,
        lldp_chassis=lldp.get("ChassisId") or None,
    )


def is_inactive_image(res: Mapping[str, Any]) -> bool:
    """True for a standby/backup image: ``Status.State`` ``Standby*``/``Disabled``/``Absent``, or an Id or
    Name saying backup, standby, inactive, pending, staged, recovery... (OpenBMC lists the backup bank,
    Supermicro "BMC Backup", often before the running image; member order is not guaranteed)."""
    state = str((res.get("Status") or {}).get("State") or "")
    if state.lower().startswith("standby") or state in ("Disabled", "Absent", "UnavailableOffline"):
        return True
    text = f"{res.get('Id') or ''} {res.get('Name') or ''}".replace("_", " ").replace("-", " ")
    return bool(_INACTIVE_IMAGE.search(text))


def parse_firmware_inventory(members: list[Mapping[str, Any]]) -> dict[str, str]:
    """FirmwareInventory members -> ``{component: version}``.

    The running image wins: for each component the first active member is used, a standby/backup image
    only when no active one is listed. ``Previous-*`` (iDRAC rollback images) are skipped.
    """
    firmware: dict[str, str] = {}
    standby: dict[str, str] = {}
    for res in members:
        rid = str(res.get("Id") or "")
        if rid.lower().startswith("previous"):
            continue
        component = component_for(res.get("Name")) or component_for(rid)
        version = res.get("Version")
        if not component or not version:
            continue
        target = standby if is_inactive_image(res) else firmware
        target.setdefault(component, str(version))
    for component, version in standby.items():
        firmware.setdefault(component, version)
    return firmware


async def walk_redfish(
    fetch: Fetcher, *, timeout: float = 3.0, want_ports: bool = True
) -> RedfishObservation:
    """Walk one BMC. Never raises: failures become ``reachable=False`` or unknown parts."""
    try:
        root = await _fetch_root(fetch)
        systems_link = odata_path(root.get("Systems"))
        try:
            systems = await fetch(systems_link or f"{ROOT}Systems")
        except FetchError as exc:
            if systems_link is None and exc.kind == "http" and exc.status == 404:
                # PDUs, CDUs, power shelves, JBOFs: a Redfish service, but not a compute node.
                return RedfishObservation(
                    reachable=False, raw_error="HTTP 404 (service root has no Systems collection)"
                )
            raise
        members = _members(systems)
        if not members:
            return RedfishObservation(reachable=False, raw_error="Systems collection has no members")
        system = await _pick_system(fetch, members)
    except FetchError as exc:
        return RedfishObservation(reachable=False, raw_error=str(exc))

    status = system.get("Status") or {}
    obs = RedfishObservation(
        reachable=True,
        health=status.get("Health"),
        state=status.get("State"),
        power_state=system.get("PowerState"),
        serial=system.get("SerialNumber"),
        model=system.get("Model"),
        manufacturer=system.get("Manufacturer"),
        hostname=system.get("HostName"),
        bios_version=system.get("BiosVersion"),
        processor_count=(system.get("ProcessorSummary") or {}).get("Count"),
    )

    # Optional branches, bounded so one slow BMC cannot stretch the run (CONTRACT 9.4).
    try:
        async with asyncio.timeout(max(2 * timeout, timeout + 1.0)):
            await _walk_optional(fetch, root, system, obs, want_ports)
    except FetchError as exc:
        obs.raw_error = f"partial walk: {exc}"
    except TimeoutError:
        obs.raw_error = "partial walk: device deadline exceeded"
    return obs


async def _pick_system(fetch: Fetcher, members: list[str]) -> dict[str, Any]:
    """The first member, unless it declares a non-physical ``SystemType`` (DPU, Virtual, Composed...)
    and a later member is ``Physical``: member order is not guaranteed; the intent describes the host."""
    first = await fetch(members[0])
    if first.get("SystemType") in (None, "Physical"):
        return first
    for path in members[1:]:
        other = await _try_fetch(fetch, path)
        if other and other.get("SystemType") == "Physical":
            return other
    return first


async def _fetch_root(fetch: Fetcher) -> dict[str, Any]:
    try:
        return await fetch(ROOT)
    except FetchError as exc:
        if exc.kind == "http" and exc.status == 404:
            return await fetch(ROOT.rstrip("/"))
        raise


async def _walk_optional(
    fetch: Fetcher,
    root: Mapping[str, Any],
    system: Mapping[str, Any],
    obs: RedfishObservation,
    want_ports: bool,
) -> None:
    # Managers: firmware version of the BMC itself (used only when FirmwareInventory lacks a BMC entry).
    manager_fw: str | None = None
    # Prefer the system's own Links.ManagedBy (blades: the blade BMC, not the enclosure manager).
    managed_by = odata_path(_first((system.get("Links") or {}).get("ManagedBy")))
    manager = await _try_fetch(fetch, managed_by)
    managers = None if manager else await _try_fetch(fetch, odata_path(root.get("Managers")))
    if managers:
        mpaths = _members(managers)
        manager = await _try_fetch(fetch, mpaths[0]) if mpaths else None
    if manager and manager.get("FirmwareVersion"):
        manager_fw = str(manager["FirmwareVersion"])

    # UpdateService/FirmwareInventory: the baseline comparison source. 404 -> unknown, not a mismatch.
    update = await _try_fetch(fetch, odata_path(root.get("UpdateService")))
    inv_path = odata_path((update or {}).get("FirmwareInventory")) or f"{ROOT}UpdateService/FirmwareInventory"
    inventory = await _try_fetch(fetch, inv_path)
    if inventory is not None:
        member_paths = _members(inventory)
        resources = await asyncio.gather(*(_try_fetch(fetch, p) for p in member_paths))
        obs.firmware = parse_firmware_inventory([r for r in resources if r])
        obs.firmware_known = True
        if "bmc" not in obs.firmware and manager_fw:
            obs.firmware["bmc"] = manager_fw
        if "bios" not in obs.firmware and obs.bios_version:
            obs.firmware["bios"] = str(obs.bios_version)

    if not want_ports:
        return

    # Chassis -> NetworkAdapters -> Ports (new schema) or NetworkPorts (legacy).
    port_paths = await _adapter_port_paths(fetch, root)
    if not port_paths:
        # Systems/<id>/NetworkInterfaces/<id>/Ports (often links back into Chassis NetworkAdapters).
        port_paths = await _network_interface_port_paths(fetch, system)
    if not port_paths:
        # No NetworkAdapters at all: Systems/<id>/EthernetInterfaces (MAC, LinkStatus, SpeedMbps; no LLDP).
        port_paths = _members(await _try_fetch(fetch, odata_path(system.get("EthernetInterfaces"))) or {})
    # A port reached twice (two adapters linking one collection, NetworkInterfaces -> Chassis) counts once.
    port_paths = list(dict.fromkeys(p.rstrip("/") for p in port_paths))
    ports = await asyncio.gather(*(_try_fetch(fetch, p) for p in port_paths))
    obs.ports = [parse_port(p) for p in ports if p]


async def _ports_of(fetch: Fetcher, parents: list[dict[str, Any] | None]) -> list[str]:
    port_paths: list[str] = []
    for ad in parents:
        if not ad:
            continue
        ports_coll = await _try_fetch(
            fetch, odata_path(ad.get("Ports")) or odata_path(ad.get("NetworkPorts"))
        )
        if ports_coll:
            port_paths.extend(_members(ports_coll))
    return port_paths


async def _network_interface_port_paths(fetch: Fetcher, system: Mapping[str, Any]) -> list[str]:
    coll = await _try_fetch(fetch, odata_path(system.get("NetworkInterfaces")))
    if not coll:
        return []
    nics = await asyncio.gather(*(_try_fetch(fetch, p) for p in _members(coll)))
    return await _ports_of(fetch, list(nics))


async def _adapter_port_paths(fetch: Fetcher, root: Mapping[str, Any]) -> list[str]:
    chassis_coll = await _try_fetch(fetch, odata_path(root.get("Chassis")))
    if not chassis_coll:
        return []
    chassis_list = await asyncio.gather(*(_try_fetch(fetch, p) for p in _members(chassis_coll)))
    adapter_paths: list[str] = []
    for ch in chassis_list:
        if not ch:
            continue
        na_path = odata_path(ch.get("NetworkAdapters"))
        if not na_path:
            own = odata_path(ch)
            na_path = f"{own}/NetworkAdapters" if own else None
        na = await _try_fetch(fetch, na_path)
        if na:
            adapter_paths.extend(_members(na))
    adapters = await asyncio.gather(*(_try_fetch(fetch, p) for p in adapter_paths))
    return await _ports_of(fetch, list(adapters))


__all__ = [
    "COMPONENTS",
    "COMPONENT_TOKENS",
    "DictFetcher",
    "FetchError",
    "Fetcher",
    "HttpFetcher",
    "Limiter",
    "component_for",
    "is_inactive_image",
    "odata_path",
    "parse_firmware_inventory",
    "parse_port",
    "walk_redfish",
]
