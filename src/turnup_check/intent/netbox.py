# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""NetBox intent adapter (REST API, NetBox 3.x and 4.x), per CONTRACT.md section 2.1.

Devices, interfaces and cables of one site are read with ``limit=1000`` pages and ``next``
links. Roles come from ``role.slug`` (NetBox 4) or ``device_role.slug`` (NetBox 3). Management
URLs come from the custom fields ``turnup_bmc_url``, ``turnup_dcgm_url`` and ``turnup_switch_url``,
else from ``primary_ip4``. The firmware baseline comes from ``config_context.turnup.firmware_baseline``.

Virtual, LAG and bridge interfaces are left out of intent (they never carry a cable). Anything the
adapter drops or cannot map (a non-interface cable end, an unrecognised role) is recorded in
``notes``, which ``load_intent`` passes on to the report.
"""

from __future__ import annotations

from collections import Counter
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from turnup_check import INTENT_SCHEMA
from turnup_check.models import (
    Cable,
    CableEnd,
    Device,
    Intent,
    Interface,
    Mgmt,
    Rack,
    iso_z,
    speed_from_interface_type,
    utcnow,
)

PAGE_LIMIT = 1000
DCGM_PORT = 9400
CONNECT_TIMEOUT = 10.0  # cap on the TCP/TLS connect phase; reads get the full per-request timeout
# Interface types that are logical, never cabled, and so not part of the physical build.
LOGICAL_INTERFACE_TYPES = frozenset({"virtual", "lag", "bridge"})
MGMT_SWITCH_ROLES = ("leaf-switch", "spine-switch", "nvlink-switch")
SIMULATION_MARKERS = ("simulated by simhall", "simhall")

# Tolerant role mapping: NetBox role slugs operators actually use -> contract roles.
ROLE_SYNONYMS: dict[str, str] = {
    "gpu-node": "gpu-node",
    "gpu-server": "gpu-node",
    "gpu-compute": "gpu-node",
    "compute-node": "gpu-node",
    "compute-tray": "gpu-node",
    "leaf-switch": "leaf-switch",
    "leaf": "leaf-switch",
    "tor": "leaf-switch",
    "tor-switch": "leaf-switch",
    "access-switch": "leaf-switch",
    "spine-switch": "spine-switch",
    "spine": "spine-switch",
    "nvlink-switch": "nvlink-switch",
    "pdu": "pdu",
    "cdu": "cdu",
}


def slugify(text: str) -> str:
    """``GPU Node`` -> ``gpu-node``."""
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in text.strip())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")


def normalize_role(raw: str | None) -> str:
    if not raw:
        return "other"
    return ROLE_SYNONYMS.get(slugify(raw), "other")


def strip_prefix_length(address: str | None) -> str | None:
    """``10.0.0.7/24`` -> ``10.0.0.7``; IPv6 addresses are bracketed for URLs."""
    if not address:
        return None
    ip = address.split("/", 1)[0].strip()
    if not ip:
        return None
    if ":" in ip and not ip.startswith("["):
        return f"[{ip}]"
    return ip


def _get(d: Any, *keys: str, default: Any = None) -> Any:
    """Nested lookup tolerant of ``None`` along the way."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


class NetBoxIntentSource:
    """Read one site's intent from a NetBox REST API."""

    flavor = "netbox"
    site_filter = "site"
    site_endpoint = "sites"

    def __init__(
        self,
        base_url: str,
        token: str | None,
        site: str,
        *,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
        page_limit: int = PAGE_LIMIT,
    ) -> None:
        base = base_url.strip().rstrip("/")
        if base.endswith("/api"):
            base = base[: -len("/api")]
        self.base_url = base
        self.token = token
        self.site = site
        self.timeout = timeout
        self.page_limit = page_limit
        self._client = client
        self.notes: list[str] = []

    # -- HTTP ----------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Token {self.token}"
        return h

    def _rebase(self, url: str) -> str:
        """Keep ``next`` links on the configured host even when NetBox advertises another one."""
        base = urlsplit(self.base_url)
        nxt = urlsplit(url)
        if nxt.netloc == base.netloc and nxt.scheme == base.scheme:
            return url
        idx = nxt.path.find("/api/")
        path = nxt.path[idx:] if idx >= 0 else nxt.path
        return urlunsplit((base.scheme, base.netloc, base.path.rstrip("/") + path, nxt.query, ""))

    def _extra_params(self, endpoint: str) -> dict[str, Any]:
        return {}

    async def _paged(self, client: httpx.AsyncClient, endpoint: str, params: dict[str, Any]) -> list[dict]:
        url: str | None = f"{self.base_url}/api/dcim/{endpoint}/"
        query: dict[str, Any] | None = {**params, **self._extra_params(endpoint), "limit": self.page_limit}
        results: list[dict] = []
        seen: set[str] = set()
        timeout = httpx.Timeout(self.timeout, connect=min(self.timeout, CONNECT_TIMEOUT))
        while url:
            resp = await client.get(url, params=query, headers=self._headers(), timeout=timeout)
            resp.raise_for_status()
            page = resp.json()
            if isinstance(page, list):  # non-paginated API (some proxies)
                results.extend(page)
                break
            results.extend(page.get("results") or [])
            nxt = page.get("next")
            if not nxt or nxt in seen:
                break
            seen.add(nxt)
            url, query = self._rebase(nxt), None
        return results

    async def load(self) -> Intent:
        if self._client is not None:
            return await self._load(self._client)
        async with httpx.AsyncClient() as client:
            return await self._load(client)

    async def _load(self, client: httpx.AsyncClient) -> Intent:
        site_params = {self.site_filter: self.site}
        devices_raw = await self._paged(client, "devices", site_params)
        if not devices_raw:
            raise ValueError(f"{self.flavor}: no devices in site {self.site!r} at {self.base_url}")
        interfaces_raw = await self._paged(client, "interfaces", site_params)
        cables_raw = await self._paged(client, "cables", site_params)
        try:
            racks_raw = await self._paged(client, "racks", site_params)
        except httpx.HTTPError:
            racks_raw = []
        try:
            sites_raw = await self._paged(client, self.site_endpoint, {"slug": self.site})
        except httpx.HTTPError:
            sites_raw = []
        return self.build_intent(devices_raw, interfaces_raw, cables_raw, racks_raw, sites_raw)

    # -- mapping -------------------------------------------------------------------------

    def raw_role(self, dev: dict) -> str | None:
        return _get(dev, "role", "slug") or _get(dev, "device_role", "slug") or _get(dev, "role", "name")

    def role_of(self, dev: dict) -> str:
        return normalize_role(self.raw_role(dev))

    @staticmethod
    def is_simulated(sites_raw: list[dict] | None, devices_raw: list[dict]) -> bool:
        """True when the site announces a simulator ("Simulated by simhall"); a real hall never does.

        An explicit ``turnup_simulated`` custom field on the site or any device also counts.
        """
        for site in sites_raw or []:
            cf = site.get("custom_fields") or {}
            if cf.get("turnup_simulated"):
                return True
            text = f"{site.get('description') or ''} {site.get('comments') or ''}".lower()
            if any(marker in text for marker in SIMULATION_MARKERS):
                return True
        return any((dev.get("custom_fields") or {}).get("turnup_simulated") for dev in devices_raw)

    def build_intent(
        self,
        devices_raw: list[dict],
        interfaces_raw: list[dict],
        cables_raw: list[dict],
        racks_raw: list[dict] | None = None,
        sites_raw: list[dict] | None = None,
    ) -> Intent:
        devices: dict[str, Device] = {}
        baseline: dict[str, dict[str, str]] = {}
        unmapped_roles: Counter[str] = Counter()
        for dev in devices_raw:
            name = dev.get("name") or f"device-{dev.get('id')}"
            role = self.role_of(dev)
            if role == "other":
                unmapped_roles[self.raw_role(dev) or "(none)"] += 1
            model = _get(dev, "device_type", "model")
            manufacturer = _get(dev, "device_type", "manufacturer", "name")
            cf = dev.get("custom_fields") or {}
            turnup_cc = _get(dev, "config_context", "turnup") or {}
            ip = strip_prefix_length(
                _get(dev, "primary_ip4", "address") or _get(dev, "primary_ip", "address")
            )
            mgmt = Mgmt()
            if role == "gpu-node":
                mgmt.bmc_url = cf.get("turnup_bmc_url") or (f"https://{ip}" if ip else None)
                mgmt.dcgm_url = cf.get("turnup_dcgm_url") or (f"http://{ip}:{DCGM_PORT}" if ip else None)
            elif role in MGMT_SWITCH_ROLES:
                mgmt.switch_url = cf.get("turnup_switch_url") or (f"https://{ip}" if ip else None)
                proto = cf.get("turnup_protocol") or turnup_cc.get("lldp_protocol")
                if proto in ("nvue", "eapi", "snmp"):
                    mgmt.protocol = proto
            gpu_count = cf.get("turnup_gpu_count") or turnup_cc.get("gpu_count")
            fw = _get(dev, "config_context", "turnup", "firmware_baseline")
            if isinstance(fw, dict) and model:
                baseline.setdefault(model, {}).update({str(k).lower(): str(v) for k, v in fw.items()})
            devices[name] = Device(
                name=name,
                role=role,
                rack=_get(dev, "rack", "name"),
                position=dev.get("position"),
                manufacturer=manufacturer,
                model=model,
                serial=dev.get("serial") or "",
                gpu_count=int(gpu_count)
                if isinstance(gpu_count, int | str) and str(gpu_count).isdigit()
                else None,
                mgmt=mgmt,
            )

        for raw, count in sorted(unmapped_roles.items()):
            self.notes.append(f"role {raw!r} not recognised: {count} device(s) treated as other")

        iface_types: dict[tuple[str, str], str | None] = {}
        logical: Counter[str] = Counter()
        for itf in interfaces_raw:
            dev_name = _get(itf, "device", "name")
            if not dev_name or dev_name not in devices:
                continue
            itype = _get(itf, "type", "value") or (
                itf.get("type") if isinstance(itf.get("type"), str) else None
            )
            if itype in LOGICAL_INTERFACE_TYPES:
                logical[itype] += 1
                continue
            mac = itf.get("mac_address") or _get(itf, "primary_mac_address", "mac_address")
            iname = itf.get("name") or ""
            devices[dev_name].interfaces.append(Interface(name=iname, type=itype, mac=mac))
            iface_types[(dev_name, iname)] = itype
        if logical:
            kinds = ", ".join(f"{k} {n}" for k, n in sorted(logical.items()))
            self.notes.append(f"{sum(logical.values())} logical interface(s) not in intent ({kinds})")

        device_names = {str(d["id"]): d["name"] for d in devices_raw if d.get("id") and d.get("name")}
        cables: list[Cable] = []
        for cab in cables_raw:
            a = self._termination(cab, "a", device_names)
            b = self._termination(cab, "b", device_names)
            if a is None or b is None:
                ka, kb = self._termination_kind(cab, "a"), self._termination_kind(cab, "b")
                why = (
                    "an end's device is not in this site"
                    if ka.endswith("interface") and kb.endswith("interface")
                    else "not interface-to-interface"
                )
                self.notes.append(
                    f"cable {cab.get('label') or cab.get('id')} skipped: {why} (a={ka}, b={kb})"
                )
                continue
            # Put the node (or non-switch) end on `a`, the switch end on `b`, as the contract example does.
            dev_a, dev_b = devices.get(a.device), devices.get(b.device)
            if dev_a is not None and dev_a.is_switch and dev_b is not None and not dev_b.is_switch:
                a, b = b, a
            speed = speed_from_interface_type(
                iface_types.get((a.device, a.interface))
            ) or speed_from_interface_type(iface_types.get((b.device, b.interface)))
            cf_speed = (cab.get("custom_fields") or {}).get("turnup_speed_gbps")
            if isinstance(cf_speed, int | float) and cf_speed > 0:
                speed = int(cf_speed)
            cables.append(Cable(label=str(cab.get("label") or ""), a=a, b=b, speed_gbps=speed))

        racks = [Rack(name=r.get("name") or str(r.get("id"))) for r in (racks_raw or []) if r]
        if not racks:
            racks = [Rack(name=n) for n in sorted({d.rack for d in devices.values() if d.rack})]

        intent = Intent(
            schema=INTENT_SCHEMA,
            site=self.site,
            simulated=self.is_simulated(sites_raw, devices_raw),
            generated_at=iso_z(utcnow()),
            racks=racks,
            devices=list(devices.values()),
            cables=cables,
            firmware_baseline=baseline,
        )
        intent.source = self.flavor
        return intent

    @staticmethod
    def _termination_kind(cab: dict, side: str) -> str:
        """Object type of a cable end, for the skip note (e.g. ``dcim.rearport``, ``dcim.powerport``)."""
        terms = cab.get(f"{side}_terminations")
        if isinstance(terms, list) and terms:
            return terms[0].get("object_type") or "unknown"
        legacy = cab.get(f"termination_{side}_type")
        if legacy:
            return str(legacy)
        if cab.get(f"termination_{side}"):
            return "unknown"
        return "none"

    @staticmethod
    def _termination(cab: dict, side: str, device_names: dict[str, str] | None = None) -> CableEnd | None:
        """One cable end as ``device/interface``, or ``None`` when it is not an interface.

        ``device_names`` (device id -> name) resolves ends whose ``device`` is a bare reference, as
        Nautobot returns at ``depth=1``.
        """
        terms = cab.get(f"{side}_terminations")
        obj: Any = None
        if isinstance(terms, list) and terms:
            first = terms[0]
            otype = first.get("object_type") or ""
            if otype and not otype.endswith("interface"):
                return None
            obj = first.get("object")
        if obj is None:
            legacy_type = cab.get(f"termination_{side}_type") or ""
            if legacy_type and not legacy_type.endswith("interface"):
                return None
            obj = cab.get(f"termination_{side}")
        if not isinstance(obj, dict):
            return None
        dev_name = _get(obj, "device", "name")
        if not dev_name and device_names:
            dev_name = device_names.get(str(_get(obj, "device", "id")))
        if_name = obj.get("name")
        if not dev_name or not if_name:
            return None
        return CableEnd(device=dev_name, interface=if_name)
