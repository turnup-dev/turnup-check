# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Shared test helpers (imported by the unit tests as ``helpers``).

Fixture loading, the output glyph helper ``G`` and a fake collector built from an intent (all-correct
truth plus mutators that mirror simhall's fault kinds).
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from turnup_check.collectors import CollectorError
from turnup_check.collectors.dcgm import parse_dcgm
from turnup_check.collectors.redfish import DictFetcher, walk_redfish
from turnup_check.models import (
    Cable,
    Creds,
    DcgmObservation,
    Device,
    Intent,
    LldpNeighbor,
    PortObservation,
    RedfishObservation,
)
from turnup_check.report import ARROW, CHECK, CROSS, DOT

FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(FIXTURES))
from make_fixtures import dcgm_text, redfish_node7  # noqa: E402


def G(text: str) -> str:
    """ASCII stand-ins -> the CLI glyphs: ``[ok]`` check, ``[x]`` cross, ``[.]`` middle dot, ``->`` arrow."""
    return text.replace("[ok]", CHECK).replace("[x]", CROSS).replace("[.]", DOT).replace("->", ARROW)


def load_json(rel: str) -> Any:
    return json.loads((FIXTURES / rel).read_text(encoding="utf-8"))


def load_text(rel: str) -> str:
    return (FIXTURES / rel).read_text(encoding="utf-8")


class FakeCollector:
    """Collector protocol over in-memory truth derived from an intent.

    ``FakeCollector.from_intent(intent)`` is an all-correct hall. Mutators deviate the physical state
    the way simhall faults do, so diff tests can state "K faults -> exactly these findings".
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, list[LldpNeighbor]]] = {}  # switch -> port -> rows
        self.lldp_errors: dict[str, str] = {}
        self.redfish_obs: dict[str, RedfishObservation] = {}
        self.redfish_trees: dict[str, dict] = {}  # alternative: walk a recorded tree
        self.dcgm_obs: dict[str, DcgmObservation] = {}
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls: list[tuple[str, str]] = []
        self.delay = 0.0
        self.strict_ports = False  # True: ports come back only with want_ports (real-collector behaviour)

    # -- construction --------------------------------------------------------------------

    @classmethod
    def from_intent(cls, intent: Intent) -> FakeCollector:
        fc = cls()
        for sw in intent.switches:
            fc.rows[sw.name] = {
                itf.name: [LldpNeighbor(switch=sw.name, local_port=itf.name, link_up=False)]
                for itf in sw.interfaces
            }
        for cable in intent.cables:
            node_end, sw_end = cable.a, cable.b
            dev_b = intent.device(sw_end.device)
            if dev_b is None or not dev_b.is_switch:
                node_end, sw_end = cable.b, cable.a
            node = intent.device(node_end.device)
            chassis = node.interfaces[0].mac if node and node.interfaces and node.interfaces[0].mac else None
            fc.rows.setdefault(sw_end.device, {})[sw_end.interface] = [
                LldpNeighbor(
                    switch=sw_end.device,
                    local_port=sw_end.interface,
                    link_up=True,
                    speed_gbps=cable.speed_gbps,
                    remote_system=node_end.device,
                    remote_port=node_end.interface,
                    remote_port_is_mac=False,
                    remote_chassis=chassis,
                )
            ]
        for node in intent.nodes:
            fc.redfish_obs[node.name] = fc._healthy_redfish(intent, node)
            fc.dcgm_obs[node.name] = parse_dcgm(dcgm_text(node.name, node.expected_gpus))
        return fc

    @staticmethod
    def _healthy_redfish(intent: Intent, node: Device) -> RedfishObservation:
        ports = []
        for itf in node.interfaces:
            sw, port = FakeCollector._intended(intent, node.name, itf.name)
            ports.append(
                PortObservation(
                    id=itf.name,
                    name=itf.name,
                    mac=itf.mac,
                    link_status="LinkUp",
                    speed_gbps=400,
                    lldp_system=sw,
                    lldp_port=port,
                )
            )
        return RedfishObservation(
            reachable=True,
            health="OK",
            state="Enabled",
            power_state="On",
            serial=node.serial or None,
            model=node.model,
            manufacturer=node.manufacturer,
            hostname=node.name,
            bios_version=intent.baseline_for(node).get("bios"),
            firmware=dict(intent.baseline_for(node)),
            firmware_known=True,
            ports=ports,
        )

    @staticmethod
    def _intended(intent: Intent, node: str, ifname: str) -> tuple[str | None, str | None]:
        for c in intent.cables:
            if (c.a.device, c.a.interface) == (node, ifname):
                return c.b.device, c.b.interface
            if (c.b.device, c.b.interface) == (node, ifname):
                return c.a.device, c.a.interface
        return None, None

    @staticmethod
    def _cable(intent: Intent, label: str) -> Cable:
        for c in intent.cables:
            if c.label == label:
                return c
        raise KeyError(label)

    @staticmethod
    def _ends(intent: Intent, cable: Cable) -> tuple[tuple[str, str], tuple[str, str]]:
        """((node, ifname), (switch, port))."""
        dev_b = intent.device(cable.b.device)
        if dev_b is not None and dev_b.is_switch:
            return (cable.a.device, cable.a.interface), (cable.b.device, cable.b.interface)
        return (cable.b.device, cable.b.interface), (cable.a.device, cable.a.interface)

    def _port_rows(self, switch: str, port: str) -> list[LldpNeighbor]:
        return self.rows.setdefault(switch, {}).setdefault(
            port, [LldpNeighbor(switch=switch, local_port=port, link_up=False)]
        )

    def _set_empty(self, switch: str, port: str, *, link_up: bool = False) -> None:
        self.rows.setdefault(switch, {})[port] = [
            LldpNeighbor(switch=switch, local_port=port, link_up=link_up)
        ]

    def _set_node_port(self, node: str, ifname: str, **changes: Any) -> None:
        rf = self.redfish_obs.get(node)
        if rf is None:
            return
        for p in rf.ports:
            if p.id == ifname:
                for k, v in changes.items():
                    setattr(p, k, v)

    # -- mutators (mirror simhall faults) ------------------------------------------------

    def _find_nic(self, node: str, ifname: str) -> tuple[str, str, LldpNeighbor]:
        """Where the NIC currently shows up: (switch, port, row)."""
        for sw, ports in self.rows.items():
            for port, rows in ports.items():
                for r in rows:
                    if r.remote_system == node and r.remote_port == ifname:
                        return sw, port, r
        raise KeyError(f"{node} {ifname} not seen")

    def move_cable(self, intent: Intent, label: str, to_switch: str, to_port: str) -> None:
        cable = self._cable(intent, label)
        (node, ifname), _ = self._ends(intent, cable)
        sw, port, row = self._find_nic(node, ifname)
        self._set_empty(sw, port)
        moved = row.model_copy(update={"switch": to_switch, "local_port": to_port, "link_up": True})
        self.rows.setdefault(to_switch, {})[to_port] = [moved]
        self._set_node_port(node, ifname, lldp_system=to_switch, lldp_port=to_port)

    def swap_pair(self, intent: Intent, label_a: str, label_b: str) -> None:
        ca, cb = self._cable(intent, label_a), self._cable(intent, label_b)
        (na, ia), (sa, pa) = self._ends(intent, ca)
        (nb, ib), (sb, pb) = self._ends(intent, cb)
        ra = next(r for r in self._port_rows(sa, pa) if r.remote_port == ia)
        rb = next(r for r in self._port_rows(sb, pb) if r.remote_port == ib)
        self.rows[sa][pa] = [rb.model_copy(update={"switch": sa, "local_port": pa})]
        self.rows[sb][pb] = [ra.model_copy(update={"switch": sb, "local_port": pb})]
        self._set_node_port(na, ia, lldp_system=sb, lldp_port=pb)
        self._set_node_port(nb, ib, lldp_system=sa, lldp_port=pa)

    def unseat(self, intent: Intent, label: str, *, nic_down: bool = True) -> None:
        cable = self._cable(intent, label)
        (node, ifname), (sw, port) = self._ends(intent, cable)
        self._set_empty(sw, port)
        if nic_down:
            self._set_node_port(node, ifname, link_status="LinkDown", lldp_system=None, lldp_port=None)

    def degrade(
        self, intent: Intent, label: str, *, speed_gbps: int | None = None, down: bool = False
    ) -> None:
        cable = self._cable(intent, label)
        (_node, ifname), (sw, port) = self._ends(intent, cable)
        rows = self._port_rows(sw, port)
        for i, r in enumerate(rows):
            if r.remote_port == ifname:
                upd: dict[str, Any] = {}
                if down:
                    upd["link_up"] = False
                if speed_gbps is not None:
                    upd["speed_gbps"] = speed_gbps
                rows[i] = r.model_copy(update=upd)

    def add_neighbor(
        self,
        switch: str,
        port: str,
        system: str | None,
        port_id: str | None,
        *,
        is_mac: bool = False,
        link_up: bool = True,
        speed_gbps: int | None = 400,
        chassis: str | None = None,
    ) -> None:
        self.rows.setdefault(switch, {})[port] = [
            LldpNeighbor(
                switch=switch,
                local_port=port,
                link_up=link_up,
                speed_gbps=speed_gbps,
                remote_system=system,
                remote_port=port_id,
                remote_port_is_mac=is_mac,
                remote_chassis=chassis,
            )
        ]

    def mac_mode(self, intent: Intent, switch: str) -> None:
        """Every neighbor on ``switch`` advertises its MAC as port id (simhall ``lldp_port_id_mac``)."""
        for port, rows in self.rows.get(switch, {}).items():
            new = []
            for r in rows:
                if r.remote_port and r.remote_system:
                    dev = intent.device(r.remote_system)
                    itf = dev.interface(r.remote_port) if dev else None
                    if itf and itf.mac:
                        r = r.model_copy(update={"remote_port": itf.mac, "remote_port_is_mac": True})
                new.append(r)
            self.rows[switch][port] = new

    def switch_error(self, switch: str, message: str = "timeout after 3 s") -> None:
        self.lldp_errors[switch] = message

    def node_unreachable(self, node: str, message: str = "timeout after 3 s") -> None:
        self.redfish_obs[node] = RedfishObservation(reachable=False, raw_error=message)

    def node_partial(self, node: str) -> None:
        rf = self.redfish_obs[node]
        rf.firmware = {}
        rf.firmware_known = False

    def node_firmware(self, node: str, component: str, version: str) -> None:
        self.redfish_obs[node].firmware[component] = version

    def node_health(self, node: str, health: str) -> None:
        self.redfish_obs[node].health = health

    def node_dcgm(self, node: str, text: str) -> None:
        self.dcgm_obs[node] = parse_dcgm(text)

    def node_dcgm_unreachable(self, node: str, message: str = "HTTP 503") -> None:
        self.dcgm_obs[node] = DcgmObservation(reachable=False, raw_error=message)

    # -- Collector protocol ----------------------------------------------------------------

    async def _track(self, kind: str, name: str) -> None:
        self.calls.append((kind, name))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1

    async def redfish(
        self, node: Device, creds: Creds, timeout: float, *, want_ports: bool = False
    ) -> RedfishObservation:
        await self._track("redfish+ports" if want_ports else "redfish", node.name)
        if node.name in self.redfish_trees:
            tree = self.redfish_trees[node.name]
            return await walk_redfish(DictFetcher(tree), timeout=timeout, want_ports=want_ports)
        obs = self.redfish_obs.get(node.name)
        if obs is None:
            return RedfishObservation(reachable=False, raw_error="connection refused")
        obs = copy.deepcopy(obs)
        if self.strict_ports and not want_ports:
            obs.ports = []  # like the real collector: ports only when asked for
        return obs

    async def lldp(self, switch: Device, creds: Creds, timeout: float) -> list[LldpNeighbor]:
        await self._track("lldp", switch.name)
        if switch.name in self.lldp_errors:
            raise CollectorError(self.lldp_errors[switch.name])
        rows: list[LldpNeighbor] = []
        for port_rows in self.rows.get(switch.name, {}).values():
            rows.extend(port_rows)
        return rows

    async def dcgm(self, node: Device, timeout: float) -> DcgmObservation:
        await self._track("dcgm", node.name)
        obs = self.dcgm_obs.get(node.name)
        if obs is None:
            return DcgmObservation(reachable=False, raw_error="connection refused")
        return copy.deepcopy(obs)

    async def __aenter__(self) -> FakeCollector:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


PING_FIELDS = frozenset(
    {
        "schema",
        "install_id",
        "tool_version",
        "simulated",
        "racks_checked",
        "links_verified",
        "nodes_checked",
        "ts",
    }
)


def _counter_accepts(body: Any) -> bool:
    """The counter's validation (counter/ rejects with 422): exactly the eight fields, a UUID install id."""
    if not isinstance(body, dict) or set(body) != PING_FIELDS:
        return False
    try:
        uuid.UUID(str(body["install_id"]))
    except ValueError:
        return False
    return body["schema"] == "turnup.ping/v1" and isinstance(body["simulated"], bool)


class CounterStub:
    """A local stand-in for the public counter on a free ``127.0.0.1`` port.

    Records every POST (path, headers, decoded body) in ``received`` and answers ``status`` after
    ``delay`` seconds, so tests can show what the real ``send_ping`` puts on the wire and that a slow or
    unhappy receiver never changes the exit code.
    """

    def __init__(self, *, status: int = 202, delay: float = 0.0, strict: bool = False) -> None:
        self.status = status
        self.delay = delay
        self.strict = strict  # answer 422 like the counter: extra keys or a non-UUID install_id
        self.received: list[tuple[str, dict[str, str], Any]] = []
        self.statuses: list[int] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    body: Any = json.loads(raw) if raw else None
                except ValueError:
                    body = raw.decode("utf-8", "replace")
                stub.received.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                if stub.delay:
                    time.sleep(stub.delay)
                status = stub.status
                if stub.strict and not _counter_accepts(body):
                    status = 422
                stub.statuses.append(status)
                payload = b'{"status":"stored","counted":false}'
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass  # the client gave up (timeout test); nothing to answer

            def log_message(self, *args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.block_on_close = False  # a handler still sleeping must not delay shutdown
        self._thread = threading.Thread(target=self._server.serve_forever, name="counter-stub", daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1/ping"

    def __enter__(self) -> CounterStub:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)


__all__ = [
    "FIXTURES",
    "CounterStub",
    "FakeCollector",
    "G",
    "dcgm_text",
    "load_json",
    "load_text",
    "redfish_node7",
]
