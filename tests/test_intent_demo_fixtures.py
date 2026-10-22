# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Intent adapters over pages captured from the public NetBox 4.7.3 and Nautobot 3.2.7 demos.

The pages under ``fixtures/netbox_demo`` and ``fixtures/nautobot_demo`` were captured on 2026-10-16
(see the README.md in each directory). They are served through
``httpx.MockTransport`` so the real adapters, pagination and query parameters run unchanged.
Nautobot pages are served the way the demo serves them: page size capped at 100, ``next`` links on
the demo host, and the depth the request asks for.
"""

from __future__ import annotations

import copy
import functools
import io
import json
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from turnup_check import cli
from turnup_check.intent import INTENT_TIMEOUT, load_intent
from turnup_check.intent.nautobot import NautobotIntentSource
from turnup_check.intent.netbox import NetBoxIntentSource

FIXTURES = Path(__file__).parent / "fixtures"
NB = "http://netbox.example.test"
NAUTOBOT_DEMO = "https://demo.nautobot.com"
NAUTOBOT_MAX_PAGE = 100  # the demo caps ?limit= at 100 and pages with `next`


@functools.cache
def _page(flavor: str, name: str) -> dict:
    return json.loads((FIXTURES / f"{flavor}_demo" / f"{name}.json").read_text(encoding="utf-8"))


def page(flavor: str, name: str) -> dict:
    return copy.deepcopy(_page(flavor, name))


def _as_reference(obj: dict) -> dict:
    return {k: obj[k] for k in ("id", "object_type", "url") if k in obj}


def at_depth1(cable: dict) -> dict:
    """A depth=2 Nautobot cable as depth=1 returns it: terminations keep their own fields, but the
    objects nested in them (``device``, ``cable``, ...) become bare references."""
    out = dict(cable)
    for side in ("a", "b"):
        term = cable.get(f"termination_{side}")
        if isinstance(term, dict):
            out[f"termination_{side}"] = {
                k: _as_reference(v) if isinstance(v, dict) and "id" in v else v for k, v in term.items()
            }
    return out


class DemoApi:
    """Serves one captured site/location; records every request."""

    def __init__(self, flavor: str, site: str, *, config_context: dict | None = None) -> None:
        self.flavor, self.site = flavor, site
        self.config_context = config_context
        self.requests: list[httpx.Request] = []

    def queries(self, endpoint: str) -> list[dict[str, list[str]]]:
        return [
            parse_qs(r.url.query.decode())
            for r in self.requests
            if r.url.path.rstrip("/").endswith(f"/{endpoint}")
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        endpoint = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        q = {k: v[-1] for k, v in parse_qs(request.url.query.decode()).items()}
        if self.flavor == "netbox":
            if endpoint in ("devices", "interfaces", "cables", "racks", "sites"):
                return httpx.Response(200, json=page("netbox", f"{self.site}_{endpoint}"))
            return httpx.Response(404)
        return self._nautobot(request, endpoint, q)

    def _nautobot(self, request: httpx.Request, endpoint: str, q: dict[str, str]) -> httpx.Response:
        loc = self.site
        if endpoint == "devices":
            body = page("nautobot", f"{loc}_devices_depth2" if q.get("depth") == "2" else f"{loc}_devices")
            if self.config_context is not None and q.get("include") == "config_context":
                for dev in body["results"]:
                    dev["config_context"] = self.config_context
            return httpx.Response(200, json=body)
        if endpoint == "cables":
            body = page("nautobot", f"{loc}_cables")
            if q.get("depth") == "1":
                body["results"] = [at_depth1(c) for c in body["results"]]
            return httpx.Response(200, json=body)
        if endpoint == "interfaces":
            rows = _page("nautobot", f"{loc}_interfaces_all")["results"]
            limit = min(int(q.get("limit", NAUTOBOT_MAX_PAGE)), NAUTOBOT_MAX_PAGE)
            offset = int(q.get("offset", 0))
            nxt = None
            if offset + limit < len(rows):
                nxt = (
                    f"{NAUTOBOT_DEMO}/api/dcim/interfaces/?depth={q.get('depth', 1)}"
                    f"&limit={limit}&location={loc}&offset={offset + limit}"
                )
            body = {
                "count": len(rows),
                "next": nxt,
                "previous": None,
                "results": rows[offset : offset + limit],
            }
            return httpx.Response(200, json=copy.deepcopy(body))
        if endpoint in ("racks", "locations"):
            return httpx.Response(200, json=page("nautobot", f"{loc}_{endpoint}"))
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def load(flavor: str, site: str, **kw):
    api = DemoApi(flavor, site, **kw)
    cls = NetBoxIntentSource if flavor == "netbox" else NautobotIntentSource
    async with api.client() as client:
        src = cls(NB, "t", site, client=client)
        intent = await src.load()
    return intent, src, api


def skip_reasons(notes: list[str]) -> Counter[str]:
    """``cable X skipped: not interface-to-interface (a=dcim.frontport, b=dcim.interface)`` -> counts
    keyed ``frontport<->interface`` (order-insensitive)."""
    out: Counter[str] = Counter()
    for n in notes:
        if " skipped: " not in n:
            continue
        inner = n.rsplit("(", 1)[1].rstrip(")")
        a, b = (part.split("=", 1)[1].removeprefix("dcim.") for part in inner.split(", "))
        out["<->".join(sorted((a, b)))] += 1
    return out


# -- counts per site, as the live run reported them -------------------------------------------

NETBOX_SITES = {
    # site: (devices, raw interfaces, intent interfaces, raw cables, mapped cables, racks, roles)
    "ncsu-065": (14, 569, 128, 15, 0, 26, {"leaf-switch": 8, "other": 6}),
    "dm-buffalo": (4, 66, 64, 7, 3, 1, {"leaf-switch": 1, "pdu": 1, "other": 2}),
}
NAUTOBOT_LOCATIONS = {
    "FRA01": (75, 354, 336, 78, 56, 11, {"leaf-switch": 2, "spine-switch": 2, "other": 71}),
    "ASH01": (76, 354, 336, 74, 52, 11, {"leaf-switch": 3, "spine-switch": 2, "other": 71}),
}


@pytest.mark.parametrize(
    ("flavor", "site", "expected"),
    [("netbox", s, e) for s, e in NETBOX_SITES.items()]
    + [("nautobot", s, e) for s, e in NAUTOBOT_LOCATIONS.items()],
)
async def test_demo_counts(flavor, site, expected):
    devices, raw_ifaces, ifaces, raw_cables, cables, racks, roles = expected
    intent, src, _api = await load(flavor, site)
    assert len(intent.devices) == devices
    assert sum(len(d.interfaces) for d in intent.devices) == ifaces
    assert len(intent.cables) == cables
    assert len(intent.racks) == racks
    assert Counter(d.role for d in intent.devices) == roles
    assert intent.source == flavor and intent.site == site and intent.simulated is False
    # Every raw cable is either mapped or explained by a note.
    assert sum(skip_reasons(src.notes).values()) == raw_cables - cables
    raw_iface_page = "interfaces_all" if flavor == "nautobot" else "interfaces"
    assert _page(flavor, f"{site}_{raw_iface_page}")["count"] == raw_ifaces
    assert _page(flavor, f"{site}_cables")["count"] == raw_cables
    for c in intent.cables:  # mapped cables only reference devices and interfaces in intent
        for end in (c.a, c.b):
            assert intent.device(end.device).interface(end.interface) is not None


async def test_ncsu_065_unnamed_devices_keep_synthetic_names():
    # The demo's eight ToR switches (and one more device) have no name; they are kept as
    # device-<id>, and their interfaces (keyed by the real, null, name) are not attached.
    intent, _src, _api = await load("netbox", "ncsu-065")
    synthetic = [d for d in intent.devices if d.name.startswith("device-")]
    assert len(synthetic) == 9
    assert all(not d.interfaces for d in synthetic)


async def test_nautobot_interfaces_follow_next_on_the_demo_host():
    _intent, _src, api = await load("nautobot", "FRA01")
    offsets = [q.get("offset", ["0"])[0] for q in api.queries("interfaces")]
    assert offsets == ["0", "100", "200", "300"]
    assert all(r.url.host == "netbox.example.test" for r in api.requests)


# -- D1: manufacturer (the LLDP protocol choice) ----------------------------------------------


async def test_d1_nautobot_devices_requested_at_depth2_resolve_manufacturer():
    intent, _src, api = await load("nautobot", "ASH01")
    assert api.queries("devices")[0]["depth"] == ["2"]
    switches = {d.name: (d.manufacturer, d.lldp_protocol) for d in intent.switches}
    assert Counter(switches.values()) == {("Arista", "eapi"): 4, ("Cisco", "snmp"): 1}
    assert switches["ash01-leaf-01.infra.ntc.com"] == ("Arista", "eapi")
    assert all(d.manufacturer for d in intent.devices)


def test_d1_depth1_page_has_no_manufacturer_name():
    # The page the adapter used to request: manufacturer is a bare reference -> snmp for everything.
    src = NautobotIntentSource(NB, "t", "ASH01")
    intent = src.build_intent(page("nautobot", "ASH01_devices")["results"], [], [])
    assert {d.manufacturer for d in intent.devices} == {None}
    assert {d.lldp_protocol for d in intent.switches} == {"snmp"}


# -- D2: config_context ------------------------------------------------------------------------


async def test_d2_nautobot_devices_include_config_context_for_the_baseline():
    cc = {"turnup": {"firmware_baseline": {"BMC": "1.2.3"}}, "ntp": {"servers": []}}
    intent, _src, api = await load("nautobot", "ASH01", config_context=cc)
    assert api.queries("devices")[0]["include"] == ["config_context"]
    leaf = intent.device("ash01-leaf-01.infra.ntc.com")
    assert intent.firmware_baseline[leaf.model] == {"bmc": "1.2.3"}


def test_d2_depth2_page_without_include_has_no_config_context():
    assert all("config_context" not in d for d in _page("nautobot", "ASH01_devices_depth2")["results"])


# -- D4: role coverage (mapping unchanged; unrecognised roles are reported) --------------------


async def test_d4_unrecognised_roles_are_noted_not_remapped():
    intent, src, _api = await load("netbox", "ncsu-065")
    assert {d.role for d in intent.devices} == {"leaf-switch", "other"}
    roles = [n for n in src.notes if n.startswith("role ")]
    assert roles == [
        "role 'application-server' not recognised: 1 device(s) treated as other",
        "role 'core-switch' not recognised: 2 device(s) treated as other",
        "role 'patch-panel' not recognised: 3 device(s) treated as other",
    ]
    _intent, src, _api = await load("nautobot", "FRA01")
    assert "role 'Load Balancer' not recognised: 2 device(s) treated as other" in src.notes


# -- D6: skip notes name both ends -------------------------------------------------------------


@pytest.mark.parametrize(
    ("flavor", "site", "reasons"),
    [
        ("netbox", "ncsu-065", {"frontport<->interface": 12, "circuits.circuittermination<->rearport": 3}),
        (
            "netbox",
            "dm-buffalo",
            {"circuits.circuittermination<->interface": 2, "poweroutlet<->powerport": 2},
        ),
        ("nautobot", "FRA01", {"frontport<->interface": 18, "rearport<->rearport": 4}),
        ("nautobot", "ASH01", {"frontport<->interface": 18, "rearport<->rearport": 4}),
    ],
)
async def test_d6_skip_reasons(flavor, site, reasons):
    _intent, src, _api = await load(flavor, site)
    assert skip_reasons(src.notes) == reasons


async def test_d6_power_cable_note_text():
    _intent, src, _api = await load("netbox", "dm-buffalo")
    power = [n for n in src.notes if "powerport" in n]
    assert len(power) == 2
    assert all(
        n.endswith("skipped: not interface-to-interface (a=dcim.powerport, b=dcim.poweroutlet)")
        for n in power
    )


def test_d6_interface_cable_to_a_device_outside_the_site_says_so():
    src = NautobotIntentSource(NB, "t", "FRA01")
    cable = {
        "id": "c1",
        "label": "X1",
        "termination_a_type": "dcim.interface",
        "termination_a": {"name": "eth0", "device": {"id": "elsewhere"}},
        "termination_b_type": "dcim.interface",
        "termination_b": {"name": "eth1", "device": {"id": "d1"}},
    }
    src.build_intent([{"id": "d1", "name": "leaf1", "role": {"name": "leaf"}}], [], [cable])
    assert src.notes == [
        "cable X1 skipped: an end's device is not in this site (a=dcim.interface, b=dcim.interface)"
    ]


def test_d6_missing_termination_is_named_none():
    src = NetBoxIntentSource(NB, "t", "s")
    src.build_intent([], [], [{"id": 9, "a_terminations": [], "b_terminations": []}])
    assert src.notes == ["cable 9 skipped: not interface-to-interface (a=none, b=none)"]


async def test_d6_adapter_notes_reach_the_report(tmp_path, monkeypatch):
    report = await _cli_run(tmp_path, monkeypatch, "netbox", "dm-buffalo")
    notes = report["notes"]
    assert sum(1 for n in notes if "not interface-to-interface (a=" in n) == 4
    assert "2 logical interface(s) not in intent (lag 2)" in notes


# -- D7: location filter -----------------------------------------------------------------------


async def test_d7_every_nautobot_query_is_filtered_by_location():
    fra, _s, api = await load("nautobot", "FRA01")
    for r in api.requests:
        q = parse_qs(r.url.query.decode())
        assert "site" not in q
        if r.url.path.endswith("/locations/"):
            assert q["name"] == ["FRA01"]
        else:  # first pages and the demo's own `next` links
            assert q["location"] == ["FRA01"]
    ash, _s, _api = await load("nautobot", "ASH01")
    assert {d.name for d in fra.devices}.isdisjoint({d.name for d in ash.devices})


# -- D8: logical interfaces --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flavor", "site", "note"),
    [
        ("netbox", "dm-buffalo", "2 logical interface(s) not in intent (lag 2)"),
        ("nautobot", "FRA01", "18 logical interface(s) not in intent (virtual 18)"),
        ("nautobot", "ASH01", "18 logical interface(s) not in intent (virtual 18)"),
    ],
)
async def test_d8_virtual_lag_bridge_interfaces_are_not_in_intent(flavor, site, note):
    intent, src, _api = await load(flavor, site)
    assert note in src.notes
    assert not {i.type for d in intent.devices for i in d.interfaces} & {"virtual", "lag", "bridge"}


def test_d8_bridge_is_excluded_too():
    src = NetBoxIntentSource(NB, "t", "s")
    dev = {"id": 1, "name": "leaf1", "role": {"slug": "leaf"}}
    ifaces = [
        {"device": {"name": "leaf1"}, "name": "br0", "type": {"value": "bridge"}},
        {"device": {"name": "leaf1"}, "name": "swp1", "type": {"value": "400gbase-x-osfp"}},
    ]
    intent = src.build_intent([dev], ifaces, [])
    assert [i.name for i in intent.device("leaf1").interfaces] == ["swp1"]


# -- D9: nvlink-switch management URL ----------------------------------------------------------


def test_d9_nvlink_switch_gets_switch_url_and_protocol():
    dev = copy.deepcopy(_page("netbox", "dm-buffalo_devices")["results"][0])
    dev.update(
        name="nvsw1",
        role={"slug": "nvlink-switch"},
        primary_ip4={"address": "10.9.0.5/24"},
        custom_fields={"turnup_protocol": "nvue"},
    )
    intent = NetBoxIntentSource(NB, "t", "dm-buffalo").build_intent([dev], [], [])
    nv = intent.device("nvsw1")
    assert nv.role == "nvlink-switch"
    assert nv.mgmt.switch_url == "https://10.9.0.5" and nv.mgmt.protocol == "nvue"
    assert nv.mgmt.bmc_url is None and not nv.is_switch  # still not polled as a fabric switch


# -- D10: the Nautobot intent load timeout -----------------------------------------------------


async def test_d10_nautobot_cables_are_read_at_depth1_and_resolved_by_device_id():
    intent, _src, api = await load("nautobot", "FRA01")
    assert api.queries("cables")[0]["depth"] == ["1"]
    assert len(intent.cables) == 56  # same as the depth=2 capture yields
    depth2 = NautobotIntentSource(NB, "t", "FRA01").build_intent(
        page("nautobot", "FRA01_devices_depth2")["results"],
        page("nautobot", "FRA01_interfaces_all")["results"],
        page("nautobot", "FRA01_cables")["results"],
    )
    key = lambda c: (c.label, c.a.device, c.a.interface, c.b.device, c.b.interface)  # noqa: E731
    assert sorted(map(key, intent.cables)) == sorted(map(key, depth2.cables))


async def test_d10_intent_reads_get_a_long_read_timeout_and_a_short_connect_timeout():
    api = DemoApi("nautobot", "ASH01")
    async with api.client() as client:
        await load_intent("nautobot", site="ASH01", client=client, env={"NAUTOBOT_URL": NB})
    for r in api.requests:
        t = r.extensions["timeout"]
        assert t["read"] == INTENT_TIMEOUT and t["connect"] == 10.0


async def test_d10_cli_timeout_flag_reaches_intent_loading(tmp_path, monkeypatch):
    seen: list[float] = []
    report = await _cli_run(tmp_path, monkeypatch, "nautobot", "ASH01", timeout=1.0, seen=seen)
    assert seen == [INTENT_TIMEOUT]  # --timeout 1 no longer caps the intent read
    assert report["intent"]["links"] == 52 and report["intent"]["source"] == "nautobot"
    seen.clear()
    await _cli_run(tmp_path, monkeypatch, "nautobot", "ASH01", timeout=300.0, seen=seen)
    assert seen == [300.0]


async def test_d10_slow_cable_page_no_longer_fails_the_load(monkeypatch):
    # Before: a 10 s per-request timeout and --timeout not plumbed -> ReadTimeout on the cable list.
    api = DemoApi("nautobot", "ASH01")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cables/") and request.extensions["timeout"]["read"] < 60:
            raise httpx.ReadTimeout("cables took 81 s", request=request)
        return api.handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        intent = await load_intent("nautobot", site="ASH01", client=client, env={"NAUTOBOT_URL": NB})
    assert len(intent.cables) == 52


async def _cli_run(tmp_path, monkeypatch, flavor, site, *, timeout=1.0, seen=None):
    """Run the real CLI with intent served from the captured pages; no switch has a URL, so
    nothing is polled (exit 2, as the live run)."""
    api = DemoApi(flavor, site)

    async def patched(arg, **kw):
        if seen is not None:
            seen.append(kw["timeout"])
        async with api.client() as client:
            return await load_intent(arg, client=client, **kw)

    monkeypatch.setattr(cli, "load_intent", patched)
    env = {"NETBOX_URL": NB, "NETBOX_TOKEN": "t", "NAUTOBOT_URL": NB, "NAUTOBOT_TOKEN": "t"}
    out_path = tmp_path / f"report-{site}.json"
    code = await cli.run(
        cli.RunOptions(
            site=site,
            intent=flavor,
            out=str(out_path),
            timeout=timeout,
            only="lldp",
            env=env,
            cwd=tmp_path,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
    )
    assert code == cli.EXIT_NO_SOURCE
    return json.loads(out_path.read_text(encoding="utf-8"))
