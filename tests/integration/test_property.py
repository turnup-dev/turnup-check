# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Property tier (CONTRACT 9.3): random halls x random fault sets -> exactly the expected finding multiset.

Each example generates a ``HallSpec`` (1-3 racks, 2-18 nodes per rack, 4-16 ports, 1-4 leaves, nvidia or
arista, LLDP port ids by name or MAC), draws up to six faults, runs the production ``HttpCollector`` and
``engine.check`` against simhall's real FastAPI app in-process (``httpx.ASGITransport``, timeouts enforced,
no sockets) and compares the report's findings, keyed by ``(class, cable, device, interface, expected,
observed, fix)``, with the multiset a small independent model derives from the faults. Then every fault
is cleared in a random order and the same check must come back empty with exit code 0.

Expected mapping, including the compound cases (this is the reference for the diff engine):

* ``wrong_port`` to a spare port -> one ``wrong_port`` on that cable, ``observed`` = the spare port, fix
  per the adjacency rule (``one port up/down``, ``k ports up/down`` for k <= 4 on the same switch, else
  ``move cable from S'/P' to S/P``).
* ``wrong_port`` onto another node's intended port, whose own cable was displaced first (simhall refuses an
  occupied port with 409): the displaced cable reports ``missing_link`` (unseated) or ``wrong_port`` (moved to
  a spare); the mover reports ``wrong_port`` with ``observed`` = the victim's port. The victim's intended
  port showing the mover's NIC is explained by the mover's finding: no ``unexpected_link``.
* ``swap_pair`` -> two ``wrong_port`` findings, each observed at the other's port.
* ``missing_link`` -> ``missing_link``; the NIC reports ``NoLink`` (nothing seated), so the fix is
  ``seat cable <label> at S/P``, never ``check NIC``.
* ``link_degraded`` (200G, 100G or down) -> one ``link_degraded``.
* ``unreachable_node`` (timeout, http_500 or refused; scope ``all``) -> exactly one ``unreachable_node`` and
  nothing else for that node: no ``node_health_warning``, ``firmware_off_baseline``, ``gpu_*`` or
  ``nvlink_degraded`` (Redfish and the exporter are both down, so GPUs are ``unknown``). Its cables are still
  seen by the switches, so cable findings on them stand (without NIC evidence).
* ``partial_redfish`` -> no finding; it also suppresses ``firmware_off_baseline`` on that node (firmware is
  unknown, and unknown versions never fail the baseline).
* ``firmware_off_baseline`` -> one finding per (node, component); components are the baseline's keys.
* ``gpu_unhealthy(node, g)`` -> one ``gpu_unhealthy`` per GPU, unless GPU ``g`` is also ``gpu_missing``.
* ``gpu_missing`` -> one ``gpu_count_mismatch`` per node however many GPUs are missing.
* ``nvlink_degraded`` -> ``nvlink_degraded`` while more than one GPU is present (CONTRACT 3.3).
* ``node_health_warning`` -> ``node_health_warning``.
* ``lldp_port_id_mac(leaf)`` -> no finding (MAC port ids resolve through the intent's MACs).

``coolant_leak`` and ``pdu_outlet_off`` are acceptance-side faults (CONTRACT 3.1 marks their telemetry
"acceptance only") and are not drawn here.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import pytest

pytest.importorskip("simhall", reason="simhall not installed in this venv", exc_type=ImportError)
hypothesis = pytest.importorskip("hypothesis")

from hypothesis import HealthCheck, event, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from simhall import Fault, Hall, HallSpec  # noqa: E402
from simhall_helpers import run_check  # noqa: E402

from turnup_check.models import Finding, Intent, port_number  # noqa: E402

pytestmark = pytest.mark.integration

MODES = ("timeout", "http_500", "refused")
KINDS = (
    "wrong_port_spare",
    "wrong_port_displaced",
    "missing_link",
    "swap_pair",
    "link_degraded",
    "unreachable_node",
    "partial_redfish",
    "firmware_off_baseline",
    "gpu_unhealthy",
    "gpu_missing",
    "nvlink_degraded",
    "node_health_warning",
    "lldp_port_id_mac",
)
LINK_CLASSES = frozenset({"wrong_port", "missing_link", "link_degraded"})

Key = tuple[str, str | None, str | None, str | None, str | None, str | None, str | None]
Pos = tuple[str, str]  # (leaf, port name)


# -- the independent model -------------------------------------------------------------------------------


def disp(leaf: str, port: str) -> str:
    return f"{leaf}/{port_number(port)}"


def wrong_port_fix(leaf: str, port: str, seen_leaf: str, seen_port: str) -> str:
    """CONTRACT section 4, written out independently of ``turnup_check.diff``."""
    p, p2 = port_number(port), port_number(seen_port)
    if leaf == seen_leaf and p is not None and p2 is not None:
        k = p2 - p
        if k == 1:
            return "move cable one port up"
        if k == -1:
            return "move cable one port down"
        if 2 <= abs(k) <= 4:
            return f"move cable {abs(k)} ports {'up' if k > 0 else 'down'}"
    return f"move cable from {disp(seen_leaf, seen_port)} to {disp(leaf, port)}"


@dataclass
class World:
    """What the faults did to the hall, from which the expected findings follow."""

    intent: Intent
    cable_pos: dict[str, Pos | None]  # switch-side position of each intent cable; None = unseated
    degraded: dict[str, Any] = field(default_factory=dict)  # label -> 200 | 100 | "down"
    unreachable: set[str] = field(default_factory=set)
    partial: set[str] = field(default_factory=set)
    health_warning: set[str] = field(default_factory=set)
    firmware: dict[tuple[str, str], str] = field(default_factory=dict)  # (node, component) -> version
    gpu_xid: set[tuple[str, int]] = field(default_factory=set)
    gpu_missing: set[tuple[str, int]] = field(default_factory=set)
    nvlink: set[str] = field(default_factory=set)

    def expected(self) -> Counter[Key]:
        out: Counter[Key] = Counter()
        for cable in self.intent.cables:
            node, iface = cable.a.device, cable.a.interface
            leaf, port = cable.b.device, cable.b.interface
            exp = disp(leaf, port)
            pos = self.cable_pos[cable.label]
            if pos is None:
                fix = f"seat cable {cable.label} at {exp}"
                out[("missing_link", cable.label, node, iface, exp, None, fix)] += 1
            elif pos != (leaf, port):
                fix = wrong_port_fix(leaf, port, *pos)
                out[("wrong_port", cable.label, node, iface, exp, disp(*pos), fix)] += 1
            elif cable.label in self.degraded:
                out[("link_degraded", cable.label, node, iface, None, None, None)] += 1
        for node in self.intent.nodes:
            name = node.name
            if name in self.unreachable:
                out[("unreachable_node", None, name, None, None, None, None)] += 1
                continue
            if name in self.health_warning:
                out[("node_health_warning", None, name, None, "OK", "Warning", None)] += 1
            if name not in self.partial:
                baseline = self.intent.baseline_for(node)
                for (n, comp), ver in self.firmware.items():
                    if n == name and comp in baseline and ver != baseline[comp]:
                        out[("firmware_off_baseline", None, name, comp, baseline[comp], ver, None)] += 1
            missing = {g for (n, g) in self.gpu_missing if n == name}
            present = [g for g in range(node.expected_gpus) if g not in missing]
            if len(present) != node.expected_gpus:
                key: Key = (
                    "gpu_count_mismatch",
                    None,
                    name,
                    None,
                    str(node.expected_gpus),
                    str(len(present)),
                    None,
                )
                out[key] += 1
            for n, g in sorted(self.gpu_xid):
                if n == name and g in present:
                    out[("gpu_unhealthy", None, name, f"gpu{g}", None, None, None)] += 1
            if name in self.nvlink and len(present) > 1:
                out[("nvlink_degraded", None, name, None, None, None, None)] += 1
        return out


def key_of(f: Finding) -> Key:
    """Project a finding onto the fields the model pins for its class."""
    c = f.class_
    if c == "wrong_port":
        return (c, f.cable, f.device, f.interface, f.expected, f.observed, f.fix)
    if c == "missing_link":
        return (c, f.cable, f.device, f.interface, f.expected, None, f.fix)
    if c == "link_degraded":
        return (c, f.cable, f.device, f.interface, None, None, None)
    if c in ("node_health_warning", "firmware_off_baseline", "gpu_count_mismatch"):
        return (c, None, f.device, f.interface, f.expected, f.observed, None)
    if c == "gpu_unhealthy":
        return (c, None, f.device, f.interface, None, None, None)
    return (c, None, f.device, None, None, None, None)


# -- drawing a fault plan --------------------------------------------------------------------------------


def draw_plan(data: st.DataObject, intent: Intent, hall: Hall) -> tuple[World, list[Fault]]:
    """Draw up to six faults for ``hall`` and the model of what they do; faults are in injection order."""
    cables = list(intent.cables)
    by_label = {c.label: c for c in cables}
    world = World(intent=intent, cable_pos={c.label: (c.b.device, c.b.interface) for c in cables})
    free = [c.label for c in cables]  # cables no cable fault has touched yet
    leaves = [s.name for s in intent.switches]
    spares = {leaf: list(hall.spare_ports(leaf)) for leaf in leaves}
    nodes = [n.name for n in intent.nodes]
    faults: list[Fault] = []

    def take_cable() -> Any:
        label = data.draw(st.sampled_from(free), label="cable")
        free.remove(label)
        return by_label[label]

    def take_spare() -> Pos | None:
        with_spares = [leaf for leaf in leaves if spares[leaf]]
        if not with_spares:
            return None
        leaf = data.draw(st.sampled_from(with_spares), label="spare leaf")
        port = data.draw(st.sampled_from(spares[leaf]), label="spare port")
        spares[leaf].remove(port)
        return leaf, port

    def take_node() -> str:
        return data.draw(st.sampled_from(nodes), label="node")

    def take_gpu(node: str) -> int:
        dev = intent.device(node)
        assert dev is not None
        return data.draw(st.integers(0, dev.expected_gpus - 1), label="gpu")

    k = data.draw(st.integers(0, 6), label="fault count")
    for _ in range(k):
        kind = data.draw(st.sampled_from(KINDS), label="kind")
        before = len(faults)
        if kind == "wrong_port_spare" and free and any(spares.values()):
            cable, target = take_cable(), take_spare()
            assert target is not None
            faults.append(
                Fault(kind="wrong_port", cable=cable.label, to_device=target[0], to_interface=target[1])
            )
            world.cable_pos[cable.label] = target
        elif kind == "wrong_port_displaced" and len(free) >= 2:
            victim, mover = take_cable(), take_cable()
            how = data.draw(st.sampled_from(["unseat", "spare"]), label="displacement")
            target = take_spare() if how == "spare" else None
            if target is None:
                faults.append(Fault(kind="missing_link", cable=victim.label))
                world.cable_pos[victim.label] = None
            else:
                faults.append(
                    Fault(kind="wrong_port", cable=victim.label, to_device=target[0], to_interface=target[1])
                )
                world.cable_pos[victim.label] = target
            vpos: Pos = (victim.b.device, victim.b.interface)
            faults.append(
                Fault(kind="wrong_port", cable=mover.label, to_device=vpos[0], to_interface=vpos[1])
            )
            world.cable_pos[mover.label] = vpos
        elif kind == "missing_link" and free:
            cable = take_cable()
            faults.append(Fault(kind="missing_link", cable=cable.label))
            world.cable_pos[cable.label] = None
        elif kind == "swap_pair" and len(free) >= 2:
            a, b = take_cable(), take_cable()
            faults.append(Fault(kind="swap_pair", cable_a=a.label, cable_b=b.label))
            world.cable_pos[a.label] = (b.b.device, b.b.interface)
            world.cable_pos[b.label] = (a.b.device, a.b.interface)
        elif kind == "link_degraded" and free:
            cable = take_cable()
            speed = data.draw(st.sampled_from([200, 100, "down"]), label="speed")
            faults.append(Fault(kind="link_degraded", cable=cable.label, speed_gbps=speed))
            world.degraded[cable.label] = speed
        elif kind == "unreachable_node":
            node = take_node()
            mode = data.draw(st.sampled_from(MODES), label="mode")
            faults.append(Fault(kind="unreachable_node", node=node, mode=mode))
            world.unreachable.add(node)
        elif kind == "partial_redfish":
            node = take_node()
            faults.append(Fault(kind="partial_redfish", node=node))
            world.partial.add(node)
        elif kind == "firmware_off_baseline":
            node = take_node()
            dev = intent.device(node)
            assert dev is not None
            components = [c for c in sorted(intent.baseline_for(dev)) if (node, c) not in world.firmware]
            if not components:
                continue
            component = data.draw(st.sampled_from(components), label="component")
            version = f"0.0.{len(faults) + 1}-off"
            faults.append(
                Fault(kind="firmware_off_baseline", node=node, component=component, version=version)
            )
            world.firmware[(node, component)] = version
        elif kind == "gpu_unhealthy":
            node = take_node()
            gpu = take_gpu(node)
            xid = data.draw(st.sampled_from([79, 48, 31, 63]), label="xid")
            faults.append(Fault(kind="gpu_unhealthy", node=node, gpu=gpu, xid=xid))
            world.gpu_xid.add((node, gpu))
        elif kind == "gpu_missing":
            node = take_node()
            gpu = take_gpu(node)
            faults.append(Fault(kind="gpu_missing", node=node, gpu=gpu))
            world.gpu_missing.add((node, gpu))
        elif kind == "nvlink_degraded":
            node = take_node()
            faults.append(Fault(kind="nvlink_degraded", node=node))
            world.nvlink.add(node)
        elif kind == "node_health_warning":
            node = take_node()
            faults.append(Fault(kind="node_health_warning", node=node))
            world.health_warning.add(node)
        elif kind == "lldp_port_id_mac":
            leaf = data.draw(st.sampled_from(leaves), label="leaf")
            faults.append(Fault(kind="lldp_port_id_mac", leaf=leaf))
        if len(faults) > before:
            event(f"drawn: {kind}")  # shows up in --hypothesis-show-statistics
    per_node = Counter(f.node for f in faults if f.node)
    event(f"faults injected: {len(faults)}")
    event(f"nodes with several faults: {sum(1 for n in per_node.values() if n > 1)}")
    return world, faults


hall_specs = st.fixed_dictionaries(
    {
        "racks": st.integers(1, 3),
        "nodes_per_rack": st.integers(2, 18),
        "ports_per_node": st.integers(4, 16),
        "leaves_per_rack": st.integers(1, 4),
        "switch_vendor": st.sampled_from(["nvidia", "arista"]),
        "lldp_port_id_mode": st.sampled_from(["name", "mac"]),
        "seed": st.integers(0, 2**31 - 1),
    }
)


def _debug(faults: list[Fault], report: Any) -> dict[str, Any]:
    return {
        "faults": [f.summary() for f in faults],
        "findings": [f.model_dump(by_alias=True, exclude={"evidence"}) for f in report.findings],
        "notes": report.notes,
    }


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.filter_too_much],
)
@given(spec_kw=hall_specs, data=st.data())
def test_random_hall_and_faults_give_exactly_the_expected_findings(
    spec_kw: dict[str, Any], data: st.DataObject
):
    spec = HallSpec(site="hall-p", **spec_kw)
    hall = Hall.generate(spec)
    intent = Intent.model_validate(hall.intent())
    assert intent.simulated is True
    world, faults = draw_plan(data, intent, hall)
    ids = [hall.inject(f).id for f in faults]

    report, _result, _obs = run_check(hall)
    expected = world.expected()
    got = Counter(key_of(f) for f in report.findings if f.class_ != "unexpected_link")
    assert got == expected, _debug(faults, report)
    assert not [f for f in report.findings if f.class_ == "unexpected_link"], _debug(faults, report)
    link_findings = sum(1 for key in expected.elements() if key[0] in LINK_CLASSES)
    assert report.summary.links_ok == len(intent.cables) - link_findings
    assert report.summary.links_total == len(intent.cables)
    assert report.summary.nodes_reachable == len(intent.nodes) - len(world.unreachable)
    assert report.summary.nodes_total == len(intent.nodes)
    assert report.exit_code == (1 if expected else 0)

    for fid in data.draw(st.permutations(ids), label="clear order") if ids else []:
        hall.clear(fid)
    after, _, _ = run_check(hall)
    assert after.findings == [], _debug([], after)
    assert after.exit_code == 0
    assert after.summary.links_ok == len(intent.cables)
    assert after.summary.nodes_reachable == len(intent.nodes)
    assert (
        after.summary.gpus_seen == after.summary.gpus_expected == sum(n.expected_gpus for n in intent.nodes)
    )
