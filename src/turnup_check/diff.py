# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""The diff engine: intent versus observations -> findings (CONTRACT.md section 4).

Link findings are correlated across the NIC and both switch ports: a NIC observed on port P' of
switch S' when intent says P of S is one ``wrong_port`` finding on the cable, which also explains
the empty intended port and the neighbor on P'. ``unexpected_link`` is only raised for neighbors no
cable finding claims.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from turnup_check.models import (
    Cable,
    Device,
    Finding,
    Intent,
    Interface,
    LldpNeighbor,
    Observations,
    Summary,
    node_iface_display,
    normalize_mac,
    port_number,
    port_suffix,
    switch_port_display,
)

SEVERITY: dict[str, str] = {
    "wrong_port": "high",
    "missing_link": "high",
    "unexpected_link": "info",
    "link_degraded": "medium",
    "unreachable_node": "high",
    "firmware_off_baseline": "medium",
    "gpu_count_mismatch": "high",
    "gpu_unhealthy": "high",
    "nvlink_degraded": "medium",
    "node_health_warning": "medium",
}

Endpoint = tuple[str, str]  # (device name, interface name)
PortKey = tuple[str, str]  # (switch name, local port name)


@dataclass
class DiffResult:
    findings: list[Finding]
    summary: Summary
    notes: list[str] = field(default_factory=list)
    # Nodes whose LLDP neighbors resolved to the device but not to a port (MAC ids, no MACs in intent):
    # the Redfish NIC ports (their MACs) are fetched in the second pass even without a link finding.
    unresolved_nodes: set[str] = field(default_factory=set)

    @property
    def counted(self) -> list[Finding]:
        return [f for f in self.findings if f.severity != "info"]


# --------------------------------------------------------------------------------------
# Fix sentences (CONTRACT section 4)
# --------------------------------------------------------------------------------------


def _simple_port(ifname: str) -> bool:
    """True when the name carries exactly one integer (``swp21``), so adjacency arithmetic is meaningful."""
    digits = [c.isdigit() for c in ifname]
    runs = sum(1 for i, d in enumerate(digits) if d and (i == 0 or not digits[i - 1]))
    return runs == 1


def wrong_port_fix(switch: str, port: str, seen_switch: str, seen_port: str) -> str:
    """Fix text for a NIC seen on ``seen_switch/seen_port`` when intent says ``switch/port``."""
    p, p2 = port_number(port), port_number(seen_port)
    if (
        switch == seen_switch
        and p is not None
        and p2 is not None
        and _simple_port(port)
        and _simple_port(seen_port)
    ):
        k = p2 - p
        if k == 1:
            return "move cable one port up"
        if k == -1:
            return "move cable one port down"
        if 2 <= abs(k) <= 4:
            return f"move cable {abs(k)} ports {'up' if k > 0 else 'down'}"
    src = switch_port_display(seen_switch, seen_port)
    dst = switch_port_display(switch, port)
    return f"move cable from {src} to {dst}"


def missing_link_fix(cable: Cable, switch: str, port: str, node: str, ifname: str, nic_down: bool) -> str:
    if nic_down:
        return f"check NIC {ifname} on {node}"
    label = cable.label or f"{node_iface_display(node, ifname)} -> {switch_port_display(switch, port)}"
    return f"seat cable {label} at {switch_port_display(switch, port)}"


# --------------------------------------------------------------------------------------
# Resolution of LLDP neighbors to intent endpoints
# --------------------------------------------------------------------------------------


def _short_host(name: str | None) -> str | None:
    if not name:
        return None
    return name.split(".", 1)[0].strip().lower() or None


class _Index:
    """Lookups over the intent plus Redfish-reported NIC MACs."""

    def __init__(self, intent: Intent, obs: Observations) -> None:
        self.intent = intent
        self.devices_lower: dict[str, Device] = {d.name.lower(): d for d in intent.devices}
        self.iface_by_mac: dict[str, Endpoint] = {}
        for dev in intent.devices:
            for itf in dev.interfaces:
                if itf.mac:
                    self.iface_by_mac.setdefault(itf.mac, (dev.name, itf.name))
        # NIC MACs the BMCs report, so a MAC-advertising NIC resolves even when NetBox has no MACs. A
        # MAC maps to the intent interface its port matches (Id, then Name; CONTRACT 3.1), never to the
        # raw port Id: BMCs that number their ports (Id "1", Name "mlx5_0") are common.
        for node_name, rf in obs.redfish.items():
            dev = intent.device(node_name)
            if dev is None or not rf.ports:
                continue
            for itf in dev.interfaces:
                p = rf.port(itf.name)
                mac = normalize_mac(p.mac) if p is not None else None
                if mac:
                    self.iface_by_mac.setdefault(mac, (dev.name, itf.name))
        self.chassis_to_device: dict[str, str] = {}
        for mac, (dev_name, _) in self.iface_by_mac.items():
            self.chassis_to_device.setdefault(mac, dev_name)

    def device(self, system: str | None) -> Device | None:
        short = _short_host(system)
        if short and short in self.devices_lower:
            return self.devices_lower[short]
        if system and system.lower() in self.devices_lower:
            return self.devices_lower[system.lower()]
        return None

    def device_of(self, row: LldpNeighbor) -> Device | None:
        """The intent device a neighbor row names (system name, else chassis MAC), port aside."""
        dev = self.device(row.remote_system)
        if dev is None and row.remote_chassis:
            cmac = normalize_mac(row.remote_chassis)
            if cmac and cmac in self.chassis_to_device:
                dev = self.intent.device(self.chassis_to_device[cmac])
        return dev

    def resolve(self, row: LldpNeighbor) -> Endpoint | None:
        """Matching rule (CONTRACT 3.2): name on the remote system, else MAC, else unknown."""
        if not row.has_neighbor:
            return None
        dev = self.device_of(row)
        rp = row.remote_port
        if dev is not None and rp and not row.remote_port_is_mac:
            itf = dev.interface(rp) or self._interface_ci(dev, rp)
            if itf is not None:
                return (dev.name, itf.name)
        mac = normalize_mac(rp) if rp else None
        if mac and mac in self.iface_by_mac:
            return self.iface_by_mac[mac]
        return None

    @staticmethod
    def _interface_ci(dev: Device, name: str) -> Interface | None:
        low = name.lower()
        for itf in dev.interfaces:
            if itf.name.lower() == low:
                return itf
        return None


def _neighbor_text(row: LldpNeighbor, resolved: Endpoint | None) -> str | None:
    if resolved:
        return node_iface_display(*resolved)
    if not row.has_neighbor:
        return None
    sys_name = row.remote_system or row.remote_chassis or "?"
    return f"{sys_name} {row.remote_port}" if row.remote_port else str(sys_name)


def _port_evidence(rows: list[LldpNeighbor], resolved: dict[int, Endpoint | None]) -> dict[str, Any]:
    if not rows:
        return {"link_up": False, "neighbor": None}
    row = rows[0]
    ev: dict[str, Any] = {"link_up": row.link_up}
    if row.speed_gbps is not None:
        ev["speed_gbps"] = row.speed_gbps
    names = [_neighbor_text(r, resolved.get(id(r))) for r in rows if r.has_neighbor]
    ev["neighbor"] = names[0] if len(names) == 1 else (names or None)
    return ev


# --------------------------------------------------------------------------------------
# The diff
# --------------------------------------------------------------------------------------


def diff(intent: Intent, obs: Observations) -> DiffResult:
    """Compare intent with observations and produce findings, summary and notes."""
    notes: list[str] = []
    findings: list[Finding] = []
    index = _Index(intent, obs)

    # Observed switch ports and the endpoint each neighbor resolves to.
    rows_at: dict[PortKey, list[LldpNeighbor]] = {}
    resolved: dict[int, Endpoint | None] = {}
    seen_at: dict[Endpoint, list[PortKey]] = {}
    for sw_name, rows in obs.lldp.items():
        for row in rows:
            key = (sw_name, row.local_port)
            rows_at.setdefault(key, []).append(row)
            ep = index.resolve(row)
            resolved[id(row)] = ep
            if ep is not None:
                seen_at.setdefault(ep, []).append(key)

    polled_switches = set(obs.lldp) | set(obs.lldp_errors)
    # Every switch end of every cable, with the endpoint intended there. A switch-to-switch cable (peer
    # link, uplink) is intended at both ends, so neither end is ever an unexpected_link.
    intended_at: dict[PortKey, Cable] = {}
    intended_ep: dict[PortKey, Endpoint] = {}
    cable_sides: list[tuple[Cable, list[tuple[PortKey, Endpoint]]]] = []
    for cable in intent.cables:
        sides = _switch_sides(intent, cable)
        for pk, ep in sides:
            intended_at[pk] = cable
            intended_ep[pk] = ep
        cable_sides.append((cable, sides))

    def live_at(ep: Endpoint, key: PortKey) -> bool:
        return any(r.link_up and resolved.get(id(r)) == ep for r in rows_at.get(key, []))

    claimed: set[PortKey] = set()
    links_ok = 0
    links_unverified = 0
    lldp_polled = "lldp" in obs.polled
    unresolved_nodes: set[str] = set()

    for cable, sides in cable_sides:
        if not lldp_polled:
            links_unverified += 1
            continue
        if not sides:
            links_unverified += 1
            notes.append(f"cable {cable.label or '?'}: neither end is a switch in intent; not verified")
            continue
        port_key, endpoint = _pick_side(sides, rows_at, resolved, obs.lldp_errors)
        sw, port = port_key
        node, ifname = endpoint
        if sw not in polled_switches:
            links_unverified += 1
            notes.append(f"cable {cable.label or '?'}: switch {sw} was not polled; not verified")
            continue
        sw_error = obs.lldp_errors.get(sw)
        here = rows_at.get(port_key, [])
        matches = [r for r in here if resolved.get(id(r)) == endpoint]
        match = next((r for r in matches if r.link_up), matches[0] if matches else None)
        expected_disp = switch_port_display(sw, port)
        nic_disp = node_iface_display(node, ifname)
        rf_port = _redfish_port(obs, intent, node, ifname)
        sightings = [k for k in seen_at.get(endpoint, []) if k != port_key]
        if match is not None and not match.link_up:
            # A down neighbor on the intended port while the NIC is live elsewhere is an aging LLDP
            # entry (TTL 120 s) left behind by a move: the live sighting is the truth.
            live = [k for k in sightings if live_at(endpoint, k)]
            if live:
                match, sightings = None, live

        if match is not None:
            speed_ok = (
                cable.speed_gbps is None or match.speed_gbps is None or match.speed_gbps >= cable.speed_gbps
            )
            if match.link_up and speed_ok:
                links_ok += 1
                continue
            if not match.link_up:
                diagnosis = "neighbor correct, port down"
                observed = f"{expected_disp} down"
                fix = f"check cable {cable.label or nic_disp} at {expected_disp} and NIC {ifname} on {node}"
            else:
                diagnosis = f"neighbor correct, speed {match.speed_gbps}G below intent {cable.speed_gbps}G"
                observed = f"{expected_disp} {match.speed_gbps}G"
                fix = (
                    f"check transceivers and cable {cable.label or nic_disp} at {expected_disp} "
                    f"({match.speed_gbps}G negotiated, intent {cable.speed_gbps}G)"
                )
            ev = {expected_disp: _port_evidence(here, resolved)}
            if rf_port is not None:
                ev[nic_disp] = rf_port
            findings.append(
                _finding(
                    "link_degraded",
                    cable=cable.label,
                    device=node,
                    interface=ifname,
                    expected=f"{expected_disp} up" + (f" {cable.speed_gbps}G" if cable.speed_gbps else ""),
                    observed=observed,
                    diagnosis=diagnosis,
                    fix=fix,
                    evidence=ev,
                )
            )
            continue

        if sightings:
            # Correlated wrong_port (CONTRACT 4), also when the intended switch itself is unreadable:
            # the NIC seen on another polled port is a move the check can see.
            sightings.sort(key=lambda k: (not live_at(endpoint, k), k))
            seen_sw, seen_port = sightings[0]
            claimed.add((seen_sw, seen_port))
            observed_disp = switch_port_display(seen_sw, seen_port)
            intended_ev: dict[str, Any] = (
                {"lldp": "unreadable", "error": sw_error} if sw_error else _port_evidence(here, resolved)
            )
            ev = {
                expected_disp: intended_ev,
                observed_disp: _port_evidence(rows_at.get((seen_sw, seen_port), []), resolved),
            }
            if rf_port is not None:
                ev[nic_disp] = rf_port
            findings.append(
                _finding(
                    "wrong_port",
                    cable=cable.label,
                    device=node,
                    interface=ifname,
                    expected=expected_disp,
                    observed=observed_disp,
                    diagnosis="neighbor mismatch"
                    + (f" ({sw} lldp unreadable; seen on {observed_disp})" if sw_error else ""),
                    fix=wrong_port_fix(sw, port, seen_sw, seen_port),
                    evidence=ev,
                )
            )
            continue

        if sw_error:
            links_unverified += 1  # reported once as the switch's unreachable_node below
            continue

        # The intended port shows a neighbor from the intended device whose port id cannot be resolved
        # (MAC-advertising NIC, no MAC in intent, BMC ports unknown): the cable may well be right. Not
        # verified, never "seat cable" on a port that shows the device.
        occupied = [r for r in here if r.has_neighbor]
        same_device = [
            r
            for r in occupied
            if resolved.get(id(r)) is None
            and (d := index.device_of(r)) is not None
            and d.name.lower() == node.lower()
        ]
        if same_device:
            links_unverified += 1
            claimed.add(port_key)
            rf_node = obs.redfish.get(node)
            if rf_node is None or rf_node.reachable:
                unresolved_nodes.add(node)
            why = "BMC unreachable" if rf_node is not None and not rf_node.reachable else "no MAC known"
            notes.append(
                f"cable {cable.label or '?'}: {expected_disp} shows {node} port "
                f"{same_device[0].remote_port or '?'}, which does not resolve to an interface "
                f"({why}); {nic_disp} not verified"
            )
            continue

        # Not seen anywhere. The fix depends on what the NIC itself reports (CONTRACT section 4):
        # ``LinkDown`` (cable present at the NIC, no link) -> check the NIC; ``NoLink`` (nothing
        # seated at the NIC) or no port evidence -> seat the cable.
        nic_status = str((rf_port or {}).get("link_status") or "")
        nic_down = nic_status.lower() == "linkdown"
        if occupied:
            other = _neighbor_text(occupied[0], resolved.get(id(occupied[0])))
            diagnosis = f"{expected_disp} shows {other}; {nic_disp} not seen on any polled switch"
            observed = f"{expected_disp} {other}"
        elif here and here[0].link_up:
            diagnosis = (
                f"{expected_disp} is up without LLDP neighbor; {nic_disp} not seen on any polled switch"
            )
            observed = f"{expected_disp} up, no neighbor"
        else:
            diagnosis = f"no neighbor on {expected_disp}; {nic_disp} not seen on any polled switch"
            observed = f"{expected_disp} no link"
        if rf_port and rf_port.get("link_up") is False:
            diagnosis += f"; {nic_disp} reports {nic_status or 'no link'}"
        ev = {expected_disp: _port_evidence(here, resolved)}
        if rf_port is not None:
            ev[nic_disp] = rf_port
        findings.append(
            _finding(
                "missing_link",
                cable=cable.label,
                device=node,
                interface=ifname,
                expected=expected_disp,
                observed=observed,
                diagnosis=diagnosis,
                fix=missing_link_fix(cable, sw, port, node, ifname, nic_down),
                evidence=ev,
            )
        )

    # Nodes: Redfish, firmware, DCGM.
    nodes = intent.nodes
    nodes_reachable = 0
    firmware_ok = True
    firmware_compared = 0
    gpus_expected = 0
    gpus_seen = 0
    redfish_polled = "redfish" in obs.polled
    dcgm_polled = "dcgm" in obs.polled
    for node in nodes:
        gpus_expected += node.expected_gpus
        if redfish_polled:
            rf = obs.redfish.get(node.name)
            if rf is None or not rf.reachable:
                err = (rf.raw_error if rf else None) or "not polled"
                findings.append(
                    _finding(
                        "unreachable_node",
                        device=node.name,
                        expected="redfish reachable",
                        observed=err,
                        diagnosis=f"redfish unreachable: {err}",
                        fix=f"check BMC reachability and credentials for {node.name}",
                        evidence={"bmc_url": node.mgmt.bmc_url, "error": err},
                    )
                )
            else:
                nodes_reachable += 1
                if rf.health and rf.health.upper() != "OK":
                    findings.append(
                        _finding(
                            "node_health_warning",
                            device=node.name,
                            expected="OK",
                            observed=rf.health,
                            diagnosis=f"redfish Status.Health {rf.health}"
                            + (f", State {rf.state}" if rf.state else ""),
                            fix=f"read the BMC event log on {node.name} before burn-in",
                            evidence={"health": rf.health, "state": rf.state, "power_state": rf.power_state},
                        )
                    )
                baseline = intent.baseline_for(node)
                if not baseline and intent.firmware_baseline:
                    notes.append(
                        f"{node.name}: no firmware baseline for model {node.model!r}; firmware not compared"
                    )
                if baseline and not rf.firmware_known:
                    notes.append(
                        f"{node.name}: firmware unknown (FirmwareInventory unavailable); "
                        "baseline not compared"
                    )
                for component, want in baseline.items():
                    got = rf.firmware.get(component)
                    if got is None:
                        if rf.firmware_known:
                            notes.append(
                                f"{node.name}: firmware {component} unknown; baseline {want} not compared"
                            )
                        continue
                    firmware_compared += 1
                    if got.strip() != want.strip():
                        firmware_ok = False
                        findings.append(
                            _finding(
                                "firmware_off_baseline",
                                device=node.name,
                                interface=component,
                                expected=want,
                                observed=got,
                                diagnosis=f"{component} {got} differs from baseline {want}",
                                fix=f"update {component} on {node.name} to {want}",
                                evidence={
                                    "component": component,
                                    "baseline": want,
                                    "observed": got,
                                    "model": node.model,
                                },
                            )
                        )
        if dcgm_polled:
            dc = obs.dcgm.get(node.name)
            if dc is None or not dc.reachable:
                notes.append(
                    f"{node.name}: dcgm unreachable ({(dc.raw_error if dc else None) or 'not polled'}); "
                    "gpu: unknown"
                )
            else:
                seen = dc.gpu_count or 0
                gpus_seen += seen
                if seen != node.expected_gpus:
                    findings.append(
                        _finding(
                            "gpu_count_mismatch",
                            device=node.name,
                            expected=str(node.expected_gpus),
                            observed=str(seen),
                            diagnosis=f"DCGM reports {seen} GPUs, intent expects {node.expected_gpus}",
                            fix=f"reseat or replace the missing GPU on {node.name}"
                            if seen < node.expected_gpus
                            else f"correct gpu_count for {node.name} in intent or remove the extra GPU",
                            evidence={"expected": node.expected_gpus, "seen": seen, "gpus": sorted(dc.raw)},
                        )
                    )
                for gpu in dc.unhealthy_gpus:
                    metrics = dc.raw.get(str(gpu), {})
                    findings.append(
                        _finding(
                            "gpu_unhealthy",
                            device=node.name,
                            interface=f"gpu{gpu}",
                            expected="healthy",
                            observed=_gpu_diagnosis(metrics),
                            diagnosis=f"GPU {gpu}: {_gpu_diagnosis(metrics)}",
                            fix=f"drain {node.name} and open an RMA packet for GPU {gpu}",
                            evidence={"gpu": gpu, "metrics": metrics},
                        )
                    )
                if dc.nvlink_degraded:
                    findings.append(
                        _finding(
                            "nvlink_degraded",
                            device=node.name,
                            expected="nvlink healthy",
                            observed="nvlink degraded",
                            diagnosis=(
                                "NVLink CRC FLIT errors above 100 or zero NVLink bandwidth "
                                "with more than one GPU"
                            ),
                            fix=f"check NVLink on {node.name} and rerun the bandwidth test",
                            evidence={
                                str(g): {k: v for k, v in m.items() if "NVLINK" in k}
                                for g, m in dc.raw.items()
                            },
                        )
                    )

    # Switches whose LLDP table could not be read.
    for sw_name, err in obs.lldp_errors.items():
        proto = obs.lldp_protocols.get(sw_name, "lldp")
        n_links = sum(1 for _, sides in cable_sides if any(pk[0] == sw_name for pk, _ in sides))
        findings.append(
            _finding(
                "unreachable_node",
                device=sw_name,
                expected="lldp table readable",
                observed=err,
                diagnosis=f"lldp unreachable ({proto}): {err}; {n_links} links not verified",
                fix=f"check switch management reachability and credentials for {sw_name}",
                evidence={"protocol": proto, "error": err, "links_unverified": n_links},
            )
        )

    # Neighbors on ports no cable finding explains.
    for key in sorted(rows_at):
        if key in claimed:
            continue
        rows = [r for r in rows_at[key] if r.has_neighbor]
        if not rows:
            continue
        cable = intended_at.get(key)
        want = intended_ep.get(key)
        for row in rows:
            ep = resolved.get(id(row))
            if ep is not None and ep == want:
                continue
            if (
                ep is not None
                and any(k == key for k in seen_at.get(ep, []))
                and _has_wrong_port(findings, ep, key)
            ):
                continue
            sw, port = key
            disp = switch_port_display(sw, port)
            if (
                not row.link_up
                and ep is not None
                and any(live_at(ep, k) for k in seen_at.get(ep, []) if k != key)
            ):
                # Aging entry on a down port for a NIC that is live elsewhere (a cable just moved).
                notes.append(
                    f"{disp}: stale LLDP entry for {node_iface_display(*ep)} (port down, ageing out)"
                )
                continue
            findings.append(
                _finding(
                    "unexpected_link",
                    cable=cable.label if cable else None,
                    device=sw,
                    interface=port,
                    expected="no neighbor" if want is None else node_iface_display(*want),
                    observed=_neighbor_text(row, ep),
                    diagnosis=f"{disp} shows {_neighbor_text(row, ep)}"
                    + (" with no cable in intent" if cable is None else ""),
                    fix=f"trace the cable at {disp} and update intent or remove it",
                    evidence={disp: _port_evidence([row], resolved)},
                )
            )

    for i, f in enumerate(findings, 1):
        f.id = f"f-{i:04d}"

    summary = Summary(
        links_ok=links_ok,
        links_total=len(intent.cables),
        nodes_reachable=nodes_reachable,
        nodes_total=len(nodes),
        firmware_ok=firmware_ok,
        gpus_expected=gpus_expected,
        gpus_seen=gpus_seen,
        findings=len(findings),
        firmware_compared=firmware_compared > 0,
    )
    if links_unverified and lldp_polled:
        notes.append(f"{links_unverified} links not verified (switch unreachable or not in intent)")
    return DiffResult(findings=findings, summary=summary, notes=notes, unresolved_nodes=unresolved_nodes)


LINK_CLASSES: frozenset[str] = frozenset({"wrong_port", "missing_link", "link_degraded"})


def nodes_needing_ports(intent: Intent, result: DiffResult) -> set[str]:
    """Nodes whose Redfish NIC port details would sharpen a link finding; fetched in a second pass."""
    nodes = {d.name for d in intent.nodes}
    linked = {f.device for f in result.findings if f.class_ in LINK_CLASSES and f.device in nodes}
    return linked | (result.unresolved_nodes & nodes)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _finding(class_: str, **kw: Any) -> Finding:
    return Finding(class_=class_, severity=SEVERITY[class_], **kw)  # type: ignore[arg-type]


def _switch_sides(intent: Intent, cable: Cable) -> list[tuple[PortKey, Endpoint]]:
    """Every ``((switch, port), (far device, far interface))`` of a cable, preferred end first.

    A node-to-switch cable has one; a switch-to-switch cable has two (either switch's LLDP verifies it).
    """
    a = (cable.a.device, cable.a.interface)
    b = (cable.b.device, cable.b.interface)
    dev_a, dev_b = intent.device(cable.a.device), intent.device(cable.b.device)
    sides: list[tuple[PortKey, Endpoint]] = []
    if dev_b is not None and dev_b.is_switch:
        sides.append((b, a))
    if dev_a is not None and dev_a.is_switch:
        sides.append((a, b))
    return sides


def _pick_side(
    sides: list[tuple[PortKey, Endpoint]],
    rows_at: dict[PortKey, list[LldpNeighbor]],
    resolved: dict[int, Endpoint | None],
    errors: dict[str, str],
) -> tuple[PortKey, Endpoint]:
    """The end to verify a cable at: one whose switch shows the far end, else one that was readable."""
    for pk, ep in sides:
        if any(resolved.get(id(r)) == ep for r in rows_at.get(pk, [])):
            return pk, ep
    for pk, ep in sides:
        if pk[0] not in errors:
            return pk, ep
    return sides[0]


def _has_wrong_port(findings: list[Finding], ep: Endpoint, key: PortKey) -> bool:
    disp = switch_port_display(*key)
    return any(
        f.class_ == "wrong_port" and (f.device, f.interface) == ep and f.observed == disp for f in findings
    )


def _redfish_port(obs: Observations, intent: Intent, node: str, ifname: str) -> dict[str, Any] | None:
    rf = obs.redfish.get(node)
    if rf is None or not rf.reachable:
        return None
    dev = intent.device(node)
    itf = dev.interface(ifname) if dev else None
    p = rf.port(ifname, itf.mac if itf else None)
    if p is None:
        return None
    ev: dict[str, Any] = {"link_up": p.link_up}
    if p.link_status:
        ev["link_status"] = p.link_status  # LinkUp | LinkDown | NoLink: picks the missing_link fix
    if p.speed_gbps is not None:
        ev["speed_gbps"] = p.speed_gbps
    if p.lldp_system or p.lldp_port:
        ev["lldp_receive"] = f"{p.lldp_system or '?'}/{port_suffix(p.lldp_port or '?')}"
    return ev


def _gpu_diagnosis(metrics: dict[str, float]) -> str:
    parts: list[str] = []
    xid = metrics.get("DCGM_FI_DEV_XID_ERRORS", 0)
    if xid > 0:
        parts.append(f"XID {int(xid)}")
    dbe = metrics.get("DCGM_FI_DEV_ECC_DBE_VOL_TOTAL", 0)
    if dbe > 0:
        parts.append(f"ECC double-bit errors {int(dbe)}")
    if metrics.get("DCGM_FI_DEV_ROW_REMAP_FAILURE", 0) > 0:
        parts.append("row remap failure")
    urr = metrics.get("DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS", 0)
    if urr > 0:
        parts.append(f"uncorrectable remapped rows {int(urr)}")
    return ", ".join(parts) or "unhealthy"


__all__ = [
    "LINK_CLASSES",
    "SEVERITY",
    "DiffResult",
    "diff",
    "missing_link_fix",
    "nodes_needing_ports",
    "wrong_port_fix",
]
