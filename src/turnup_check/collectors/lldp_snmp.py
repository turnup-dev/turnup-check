# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""LLDP via SNMP (IEEE 802.1AB LLDP-MIB ``lldpRemTable`` plus IF-MIB link state), CONTRACT 3.2.

The parser works on a walked ``(oid, value)`` table so it is unit-tested with canned tables;
the live walker uses pysnmp (optional dependency, imported lazily) and is not exercised by tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

from turnup_check.models import LldpNeighbor, normalize_mac

# LLDP-MIB (IEEE 802.1AB-2005), lldpRemEntry columns:
#   1.0.8802.1.1.2.1.4.1.1.<col>.<timeMark>.<localPortNum>.<remIndex>
LLDP_REM_TABLE = "1.0.8802.1.1.2.1.4.1.1"
REM_CHASSIS_ID_SUBTYPE = f"{LLDP_REM_TABLE}.4"
REM_CHASSIS_ID = f"{LLDP_REM_TABLE}.5"
REM_PORT_ID_SUBTYPE = f"{LLDP_REM_TABLE}.6"
REM_PORT_ID = f"{LLDP_REM_TABLE}.7"
REM_PORT_DESC = f"{LLDP_REM_TABLE}.8"
REM_SYS_NAME = f"{LLDP_REM_TABLE}.9"
# lldpLocPortEntry: 1.0.8802.1.1.2.1.3.7.1.<col>.<localPortNum>
LLDP_LOC_PORT_TABLE = "1.0.8802.1.1.2.1.3.7.1"
LOC_PORT_ID_SUBTYPE = f"{LLDP_LOC_PORT_TABLE}.2"
LOC_PORT_ID = f"{LLDP_LOC_PORT_TABLE}.3"
LOC_PORT_DESC = f"{LLDP_LOC_PORT_TABLE}.4"
# IF-MIB
IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"  # up(1) down(2)
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"  # Mbps

WALK_ROOTS: tuple[str, ...] = (LLDP_REM_TABLE, LLDP_LOC_PORT_TABLE, IF_OPER_STATUS, IF_NAME, IF_HIGH_SPEED)

# LldpPortIdSubtype / LldpChassisIdSubtype
PORT_SUBTYPE_MAC = 3
PORT_SUBTYPE_IFNAME = 5
PORT_SUBTYPE_LOCAL = 7
CHASSIS_SUBTYPE_MAC = 4

Walker = Callable[[str], Awaitable[Iterable[tuple[str, Any]]]]


def _text(value: Any) -> str:
    """OCTET STRING -> printable text (bytes decoded as UTF-8, else latin-1)."""
    if value is None:
        return ""
    if isinstance(value, bytes | bytearray):
        try:
            return bytes(value).decode("utf-8").strip("\x00")
        except UnicodeDecodeError:
            return bytes(value).decode("latin-1")
    return str(value)


def _mac(value: Any) -> str | None:
    """OCTET STRING holding a 6-byte MAC (raw, hex text or colon text) -> ``aa:bb:cc:dd:ee:ff``."""
    if isinstance(value, bytes | bytearray) and len(value) == 6:
        return ":".join(f"{b:02x}" for b in bytes(value))
    text = _text(value)
    if len(text) == 6 and not text.isprintable():
        return ":".join(f"{ord(c):02x}" for c in text)
    return normalize_mac(text)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _split(oid: str, prefix: str) -> list[str] | None:
    if oid == prefix or not oid.startswith(prefix + "."):
        return None
    return oid[len(prefix) + 1 :].split(".")


def parse_lldp_snmp(rows: Iterable[tuple[str, Any]], switch: str) -> list[LldpNeighbor]:
    """Walked ``(oid, value)`` pairs -> normalized rows for every LLDP-enabled local port."""
    loc_id: dict[int, Any] = {}
    loc_subtype: dict[int, int] = {}
    loc_desc: dict[int, str] = {}
    if_name: dict[int, str] = {}
    if_oper: dict[int, int] = {}
    if_speed: dict[int, int] = {}
    rem: dict[tuple[int, int], dict[str, Any]] = {}

    for oid, value in rows:
        oid = str(oid)
        for col, bucket in ((LOC_PORT_ID, loc_id), (LOC_PORT_DESC, loc_desc)):
            idx = _split(oid, col)
            if idx and len(idx) == 1 and idx[0].isdigit():
                bucket[int(idx[0])] = value if col == LOC_PORT_ID else _text(value)
        idx = _split(oid, LOC_PORT_ID_SUBTYPE)
        if idx and len(idx) == 1 and idx[0].isdigit():
            loc_subtype[int(idx[0])] = _int(value) or 0
        for col, bucket, conv in (
            (IF_NAME, if_name, _text),
            (IF_OPER_STATUS, if_oper, _int),
            (IF_HIGH_SPEED, if_speed, _int),
        ):
            idx = _split(oid, col)
            if idx and len(idx) == 1 and idx[0].isdigit():
                bucket[int(idx[0])] = conv(value)  # type: ignore[assignment]
        for col, key in (
            (REM_CHASSIS_ID_SUBTYPE, "chassis_subtype"),
            (REM_CHASSIS_ID, "chassis_id"),
            (REM_PORT_ID_SUBTYPE, "port_subtype"),
            (REM_PORT_ID, "port_id"),
            (REM_PORT_DESC, "port_desc"),
            (REM_SYS_NAME, "sys_name"),
        ):
            idx = _split(oid, col)
            if idx and len(idx) == 3 and all(p.isdigit() for p in idx):
                rem.setdefault((int(idx[1]), int(idx[2])), {})[key] = value

    def port_name(num: int) -> str:
        subtype = loc_subtype.get(num, 0)
        raw = loc_id.get(num)
        if raw is not None and subtype in (PORT_SUBTYPE_IFNAME, PORT_SUBTYPE_LOCAL, 0) and _mac(raw) is None:
            return _text(raw)
        if if_name.get(num):
            return if_name[num]
        if loc_desc.get(num):
            return loc_desc[num]
        return _text(raw) if raw is not None else f"port{num}"

    ports = set(loc_id) | {num for num, _ in rem}
    out: list[LldpNeighbor] = []
    for num in sorted(ports):
        name = port_name(num)
        oper = if_oper.get(num)
        mbps = if_speed.get(num)
        speed = (mbps // 1000) if mbps else None
        remotes = [rem[k] for k in sorted(rem) if k[0] == num]
        if oper is None:
            link_up = bool(remotes)
        else:
            link_up = oper == 1
        if not remotes:
            out.append(LldpNeighbor(switch=switch, local_port=name, link_up=link_up, speed_gbps=speed))
            continue
        for r in remotes:
            psub = _int(r.get("port_subtype")) or 0
            pid = r.get("port_id")
            is_mac = psub == PORT_SUBTYPE_MAC or (psub == 0 and _mac(pid) is not None)
            remote_port = _mac(pid) if is_mac else (_text(pid) or None)
            if not remote_port and r.get("port_desc"):
                remote_port = _text(r["port_desc"])
            csub = _int(r.get("chassis_subtype")) or 0
            cid = r.get("chassis_id")
            chassis = _mac(cid) if csub in (CHASSIS_SUBTYPE_MAC, 0) and _mac(cid) else (_text(cid) or None)
            out.append(
                LldpNeighbor(
                    switch=switch,
                    local_port=name,
                    link_up=link_up,
                    speed_gbps=speed,
                    remote_system=_text(r.get("sys_name")) or None,
                    remote_port=remote_port,
                    remote_port_is_mac=bool(is_mac),
                    remote_chassis=chassis,
                )
            )
    return out


def host_from_url(url: str) -> str:
    """``https://10.0.0.4`` or ``snmp://10.0.0.4:161`` -> host; a bare host is returned as is."""
    if "://" not in url:
        return url.split("/", 1)[0]
    return urlsplit(url).hostname or url


async def pysnmp_walk(host: str, community: str, timeout: float, port: int = 161) -> Walker:
    """Build a walker over pysnmp (v2c). Imported lazily: pysnmp is an optional dependency."""
    from pysnmp.hlapi.v3arch.asyncio import (  # type: ignore[import-not-found]
        CommunityData,
        ContextData,
        ObjectIdentity,
        ObjectType,
        SnmpEngine,
        UdpTransportTarget,
        bulk_walk_cmd,
    )

    engine = SnmpEngine()
    target = await UdpTransportTarget.create((host, port), timeout=timeout, retries=0)
    auth = CommunityData(community, mpModel=1)

    async def walk(root: str) -> list[tuple[str, Any]]:
        rows: list[tuple[str, Any]] = []
        gen: AsyncIterator[Any] = bulk_walk_cmd(
            engine,
            auth,
            target,
            ContextData(),
            0,
            25,
            ObjectType(ObjectIdentity(root)),
            lexicographicMode=False,
        )
        async for error_indication, error_status, _error_index, var_binds in gen:
            if error_indication:
                raise ConnectionError(str(error_indication))
            if error_status:
                raise ConnectionError(error_status.prettyPrint())
            for oid, value in var_binds:
                rows.append((str(oid), _py(value)))
        return rows

    return walk


def _py(value: Any) -> Any:
    """pysnmp value -> bytes or int."""
    if hasattr(value, "asOctets"):
        return value.asOctets()
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


async def collect_lldp_snmp(
    switch: str, host: str, community: str, timeout: float, walker: Walker | None = None
) -> list[LldpNeighbor]:
    """Walk the LLDP and IF tables of one switch and normalize them."""
    if walker is None:
        walker = await pysnmp_walk(host, community, timeout)
    rows: list[tuple[str, Any]] = []
    for root in WALK_ROOTS:
        rows.extend(await walker(root))
    return parse_lldp_snmp(rows, switch)


__all__ = [
    "IF_HIGH_SPEED",
    "IF_NAME",
    "IF_OPER_STATUS",
    "LLDP_LOC_PORT_TABLE",
    "LLDP_REM_TABLE",
    "WALK_ROOTS",
    "Walker",
    "collect_lldp_snmp",
    "host_from_url",
    "parse_lldp_snmp",
]
