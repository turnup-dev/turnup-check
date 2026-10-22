# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Data models shared by the intent adapters, collectors, diff engine and report writer.

Everything here mirrors CONTRACT.md sections 1-4. Field names are the JSON keys of the
``turnup.intent/v1`` and ``turnup.report/v1`` documents.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from turnup_check import INTENT_SCHEMA, REPORT_SCHEMA, TOOL_NAME, __version__

Role = Literal["gpu-node", "leaf-switch", "spine-switch", "nvlink-switch", "pdu", "cdu", "other"]
Protocol = Literal["nvue", "eapi", "snmp"]
SWITCH_ROLES: frozenset[str] = frozenset({"leaf-switch", "spine-switch"})
NODE_ROLE = "gpu-node"

# Finding classes, exactly the strings of CONTRACT.md section 4.
FINDING_CLASSES: tuple[str, ...] = (
    "wrong_port",
    "missing_link",
    "unexpected_link",
    "link_degraded",
    "unreachable_node",
    "firmware_off_baseline",
    "gpu_count_mismatch",
    "gpu_unhealthy",
    "nvlink_degraded",
    "node_health_warning",
)

_PORT_NUMBER = re.compile(r"(\d+)")
_SPEED_IN_TYPE = re.compile(r"(\d+)gbase", re.IGNORECASE)


def utcnow() -> datetime:
    """Current time, timezone-aware UTC."""
    return datetime.now(UTC)


def iso_z(ts: datetime) -> str:
    """ISO-8601 with a trailing ``Z`` and second precision, the form every Turnup document uses."""
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_mac(mac: str | None) -> str | None:
    """Lower-case colon-separated MAC; accepts Cisco dotted and dash forms. ``None`` when not a MAC."""
    if not mac:
        return None
    hexdigits = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(hexdigits) != 12:
        return None
    return ":".join(hexdigits[i : i + 2] for i in range(0, 12, 2)).lower()


def port_number(ifname: str) -> int | None:
    """First integer in a switch interface name: ``swp21`` -> 21, ``Ethernet21/1`` -> 21."""
    m = _PORT_NUMBER.search(ifname or "")
    return int(m.group(1)) if m else None


def port_suffix(ifname: str) -> str:
    """Numeric tail of a switch interface name: ``swp21`` -> ``21``, ``Ethernet21/1`` -> ``21/1``."""
    m = _PORT_NUMBER.search(ifname or "")
    if not m:
        return ifname
    return ifname[m.start() :]


def switch_port_display(switch: str, ifname: str) -> str:
    """Display form of a switch port: ``leaf4/21`` or ``leaf4/21/1``."""
    return f"{switch}/{port_suffix(ifname)}"


def node_iface_display(node: str, ifname: str) -> str:
    """Display form of a node interface: ``node7 mlx5_3``."""
    return f"{node} {ifname}"


def gpu_count_for_model(model: str | None) -> int:
    """Default GPU count by model substring (CONTRACT 2.1)."""
    text = (model or "").upper()
    if "NVL72" in text:
        return 4
    if "HGX" in text:
        return 8
    if any(k in text for k in ("MI300", "MI325", "MI355")):
        return 8
    return 8


def version_text(value: Any) -> str:
    """A firmware version as written. YAML reads ``bmc: 1.10`` as the float 1.1; the intent and baseline
    loaders keep the scalar's source text (``yaml_text``) so the baseline stays ``"1.10"``."""
    text = getattr(value, "yaml_text", None)
    return str(text if text is not None else value).strip()


def model_key(model: str | None) -> str:
    """Baseline lookup key for a device model: trimmed, case-folded, inner whitespace collapsed."""
    return " ".join((model or "").split()).casefold()


def protocol_for_manufacturer(manufacturer: str | None) -> Protocol:
    """LLDP transport by switch manufacturer (CONTRACT 3.2)."""
    text = (manufacturer or "").lower()
    if "arista" in text:
        return "eapi"
    if any(k in text for k in ("nvidia", "cumulus", "mellanox")):
        return "nvue"
    return "snmp"


def speed_from_interface_type(type_slug: str | None) -> int | None:
    """``400gbase-x-osfp`` -> 400; ``None`` when the type carries no speed."""
    if not type_slug:
        return None
    m = _SPEED_IN_TYPE.search(type_slug)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------------------
# Intent (turnup.intent/v1)
# --------------------------------------------------------------------------------------


class Interface(BaseModel):
    """One interface of a device in intent."""

    model_config = ConfigDict(extra="ignore")

    name: str
    type: str | None = None
    mac: str | None = None

    @field_validator("mac", mode="before")
    @classmethod
    def _norm_mac(cls, v: Any) -> str | None:
        if v in (None, ""):
            return None
        return normalize_mac(str(v)) or str(v).lower()


class Mgmt(BaseModel):
    """Management endpoints of a device. All optional; collectors skip what is missing."""

    model_config = ConfigDict(extra="ignore")

    bmc_url: str | None = None
    dcgm_url: str | None = None
    switch_url: str | None = None
    protocol: Protocol | None = None


class Device(BaseModel):
    """A device in intent (node, switch, pdu, ...)."""

    model_config = ConfigDict(extra="ignore")

    name: str
    role: str = "other"
    rack: str | None = None
    position: int | float | None = None
    manufacturer: str | None = None
    model: str | None = None
    serial: str | None = ""
    gpu_count: int | None = None
    mgmt: Mgmt = Field(default_factory=Mgmt)
    interfaces: list[Interface] = Field(default_factory=list)

    @field_validator("rack", mode="before")
    @classmethod
    def _rack_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)

    @property
    def is_node(self) -> bool:
        return self.role == NODE_ROLE

    @property
    def is_switch(self) -> bool:
        return self.role in SWITCH_ROLES

    @property
    def expected_gpus(self) -> int:
        return self.gpu_count if self.gpu_count is not None else gpu_count_for_model(self.model)

    @property
    def lldp_protocol(self) -> Protocol:
        return self.mgmt.protocol or protocol_for_manufacturer(self.manufacturer)

    def interface(self, name: str) -> Interface | None:
        for itf in self.interfaces:
            if itf.name == name:
                return itf
        return None


class Rack(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str

    @field_validator("name", mode="before")
    @classmethod
    def _name_str(cls, v: Any) -> str:
        return str(v)


class CableEnd(BaseModel):
    model_config = ConfigDict(extra="ignore")

    device: str
    interface: str


class Cable(BaseModel):
    """One intended link."""

    model_config = ConfigDict(extra="ignore")

    label: str = ""
    a: CableEnd
    b: CableEnd
    speed_gbps: int | None = None


class Intent(BaseModel):
    """The planned build (CONTRACT section 2)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    schema_: str = Field(
        default=INTENT_SCHEMA,
        validation_alias=AliasChoices("schema", "schema_"),
        serialization_alias="schema",
    )
    site: str
    simulated: bool = False
    generated_at: str | None = None
    racks: list[Rack] = Field(default_factory=list)
    devices: list[Device] = Field(default_factory=list)
    cables: list[Cable] = Field(default_factory=list)
    firmware_baseline: dict[str, dict[str, str]] = Field(default_factory=dict)
    source: str = Field(default="file", exclude=True)
    notes: list[str] = Field(default_factory=list, exclude=True)  # adapter notes, carried to the report
    _dev_cache: dict[str, Device] | None = PrivateAttr(default=None)

    @field_validator("schema_")
    @classmethod
    def _check_schema(cls, v: str) -> str:
        if v != INTENT_SCHEMA:
            raise ValueError(f"unsupported intent schema {v!r}; expected {INTENT_SCHEMA!r}")
        return v

    @field_validator("generated_at", mode="before")
    @classmethod
    def _generated_at_str(cls, v: Any) -> str | None:
        if v is None:
            return None
        if isinstance(v, datetime):  # YAML parses bare timestamps into datetimes
            return iso_z(v) if v.tzinfo else v.strftime("%Y-%m-%dT%H:%M:%SZ")
        return str(v)

    @field_validator("firmware_baseline", mode="before")
    @classmethod
    def _baseline_strings(cls, v: Any) -> Any:
        if not v:
            return {}
        out: dict[str, dict[str, str]] = {}
        for model, comps in dict(v).items():
            out[str(model)] = {str(k).lower(): version_text(val) for k, val in dict(comps or {}).items()}
        return out

    # -- lookups -----------------------------------------------------------------------

    def device(self, name: str) -> Device | None:
        return self._by_name().get(name)

    def _by_name(self) -> dict[str, Device]:
        cache = self._dev_cache
        if cache is None or len(cache) != len(self.devices):
            cache = {d.name: d for d in self.devices}
            self._dev_cache = cache
        return cache

    @property
    def nodes(self) -> list[Device]:
        return [d for d in self.devices if d.is_node]

    @property
    def switches(self) -> list[Device]:
        return [d for d in self.devices if d.is_switch]

    @property
    def leaves(self) -> list[Device]:
        return [d for d in self.devices if d.role == "leaf-switch"]

    @property
    def rack_names(self) -> list[str]:
        names = {r.name for r in self.racks}
        names.update(d.rack for d in self.devices if d.rack)
        return sorted(names)

    def baseline_for(self, device: Device) -> dict[str, str]:
        """The baseline for the device's model: exact key first, then case- and whitespace-insensitive."""
        if not device.model:
            return {}
        if device.model in self.firmware_baseline:
            return self.firmware_baseline[device.model]
        want = model_key(device.model)
        for model, comps in self.firmware_baseline.items():
            if model_key(model) == want:
                return comps
        return {}

    def summary(self) -> dict[str, Any]:
        return {
            "links": len(self.cables),
            "nodes": len(self.nodes),
            "leaves": len(self.leaves),
            "racks": len(self.rack_names),
            "source": self.source,
        }


# --------------------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------------------


class DeviceCreds(BaseModel):
    model_config = ConfigDict(extra="ignore")

    user: str | None = None
    password: str | None = None
    community: str | None = None


class Creds(BaseModel):
    """Credentials from the environment and ``--creds creds.yaml``."""

    model_config = ConfigDict(extra="ignore")

    bmc_user: str | None = None
    bmc_pass: str | None = None
    switch_user: str | None = None
    switch_pass: str | None = None
    snmp_community: str = "public"
    snmp_version: str = "2c"
    devices: dict[str, DeviceCreds] = Field(default_factory=dict)

    def bmc(self, device: str) -> tuple[str, str] | None:
        over = self.devices.get(device)
        user = (over.user if over and over.user else None) or self.bmc_user
        pw = (over.password if over and over.password is not None else None) or self.bmc_pass or ""
        return (user, pw) if user else None

    def switch(self, device: str) -> tuple[str, str] | None:
        over = self.devices.get(device)
        user = (over.user if over and over.user else None) or self.switch_user
        pw = (over.password if over and over.password is not None else None) or self.switch_pass or ""
        return (user, pw) if user else None

    def community(self, device: str) -> str:
        over = self.devices.get(device)
        return (over.community if over and over.community else None) or self.snmp_community


# --------------------------------------------------------------------------------------
# Observations (what the collectors saw)
# --------------------------------------------------------------------------------------


class LldpNeighbor(BaseModel):
    """One switch-port row of a normalized LLDP table (CONTRACT 3.2)."""

    model_config = ConfigDict(extra="ignore")

    switch: str
    local_port: str
    link_up: bool
    speed_gbps: int | None = None
    remote_system: str | None = None
    remote_port: str | None = None
    remote_port_is_mac: bool = False
    remote_chassis: str | None = None

    @property
    def has_neighbor(self) -> bool:
        return bool(self.remote_port or self.remote_system or self.remote_chassis)

    @property
    def display(self) -> str:
        return switch_port_display(self.switch, self.local_port)


class PortObservation(BaseModel):
    """A node NIC port as Redfish reports it."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str | None = None
    mac: str | None = None
    link_status: str | None = None  # LinkUp | LinkDown | NoLink | None
    speed_gbps: float | None = None
    lldp_system: str | None = None
    lldp_port: str | None = None
    lldp_chassis: str | None = None

    @property
    def link_up(self) -> bool | None:
        if self.link_status is None:
            return None
        return self.link_status.lower() == "linkup"


class RedfishObservation(BaseModel):
    """What the Redfish collector learned about one node."""

    model_config = ConfigDict(extra="ignore")

    reachable: bool
    health: str | None = None
    state: str | None = None
    power_state: str | None = None
    serial: str | None = None
    model: str | None = None
    manufacturer: str | None = None
    hostname: str | None = None
    bios_version: str | None = None
    processor_count: int | None = None
    firmware: dict[str, str] = Field(default_factory=dict)
    firmware_known: bool = False
    ports: list[PortObservation] = Field(default_factory=list)
    raw_error: str | None = None

    def port(self, ifname: str, mac: str | None = None) -> PortObservation | None:
        """Match a port by ``Id``, then ``Name``, then MAC (CONTRACT 3.1)."""
        for p in self.ports:
            if p.id == ifname:
                return p
        for p in self.ports:
            if p.name == ifname:
                return p
        nmac = normalize_mac(mac)
        if nmac:
            for p in self.ports:
                if normalize_mac(p.mac) == nmac:
                    return p
        return None


class DcgmObservation(BaseModel):
    """What the DCGM collector learned about one node."""

    model_config = ConfigDict(extra="ignore")

    reachable: bool
    gpu_count: int | None = None
    unhealthy_gpus: list[int] = Field(default_factory=list)
    nvlink_degraded: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)
    raw_error: str | None = None


class Observations(BaseModel):
    """Everything one polling pass collected, keyed by device name."""

    model_config = ConfigDict(extra="ignore")

    lldp: dict[str, list[LldpNeighbor]] = Field(default_factory=dict)
    lldp_errors: dict[str, str] = Field(default_factory=dict)
    lldp_protocols: dict[str, str] = Field(default_factory=dict)
    redfish: dict[str, RedfishObservation] = Field(default_factory=dict)
    dcgm: dict[str, DcgmObservation] = Field(default_factory=dict)
    polled: list[str] = Field(default_factory=lambda: ["lldp", "redfish", "dcgm"])

    def sources(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if "lldp" in self.polled:
            by_proto: dict[str, int] = {}
            for sw in set(self.lldp) | set(self.lldp_errors):
                proto = self.lldp_protocols.get(sw, "unknown")
                by_proto[proto] = by_proto.get(proto, 0) + 1
            for proto in sorted(by_proto):
                out.append({"kind": "lldp", "protocol": proto, "devices": by_proto[proto]})
        if "redfish" in self.polled:
            out.append({"kind": "redfish", "devices": len(self.redfish)})
        if "dcgm" in self.polled:
            out.append({"kind": "dcgm", "devices": len(self.dcgm)})
        return out

    def any_reachable(self) -> bool:
        if any(self.lldp.values()) or any(self.lldp.keys()):
            return True
        if any(o.reachable for o in self.redfish.values()):
            return True
        return any(o.reachable for o in self.dcgm.values())


# --------------------------------------------------------------------------------------
# Findings and the report (turnup.report/v1)
# --------------------------------------------------------------------------------------

Severity = Literal["high", "medium", "low", "info"]


class Finding(BaseModel):
    """One line of the diff."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str = ""
    class_: str = Field(validation_alias=AliasChoices("class", "class_"), serialization_alias="class")
    severity: Severity = "high"
    cable: str | None = None
    device: str | None = None
    interface: str | None = None
    expected: str | None = None
    observed: str | None = None
    diagnosis: str | None = None
    fix: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("class_")
    @classmethod
    def _known_class(cls, v: str) -> str:
        if v not in FINDING_CLASSES:
            raise ValueError(f"unknown finding class {v!r}")
        return v

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)


class Summary(BaseModel):
    model_config = ConfigDict(extra="allow")

    links_ok: int = 0
    links_total: int = 0
    nodes_reachable: int = 0
    nodes_total: int = 0
    firmware_ok: bool = True
    gpus_expected: int = 0
    gpus_seen: int = 0
    findings: int = 0
    # False when no firmware component was compared (no baseline for any reachable node's model). Not
    # part of the report JSON (CONTRACT 4 fixes the summary keys); the CLI says "firmware not compared".
    firmware_compared: bool = Field(default=True, exclude=True)


class Report(BaseModel):
    """``turnup.report/v1``."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    schema_: str = Field(
        default=REPORT_SCHEMA,
        validation_alias=AliasChoices("schema", "schema_"),
        serialization_alias="schema",
    )
    tool: str = TOOL_NAME
    tool_version: str = __version__
    site: str
    simulated: bool = False
    generated_at: str
    elapsed_s: float
    intent: dict[str, Any]
    summary: Summary
    findings: list[Finding]
    sources: list[dict[str, Any]]
    notes: list[str] = Field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)

    @property
    def counted_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity != "info"]

    @property
    def exit_code(self) -> int:
        return 1 if self.counted_findings else 0
