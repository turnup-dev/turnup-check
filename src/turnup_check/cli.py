# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""The ``turnup-check`` command (CONTRACT.md section 4): read intent, poll, diff, report.

Exit codes: 0 no findings, 1 findings, 2 could not read intent or no telemetry source reachable.
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, TextIO

import click
import yaml

from turnup_check import __version__
from turnup_check.collectors import Collector, HttpCollector, normalize_only
from turnup_check.engine import check
from turnup_check.intent import INTENT_TIMEOUT, IntentError, load_intent
from turnup_check.models import Creds, DeviceCreds, Intent
from turnup_check.report import (
    CROSS,
    build_report,
    emit,
    intent_line,
    polling_line,
    render_lines,
    report_json,
    write_report,
)
from turnup_check.telemetry import build_ping, send_ping

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_NO_SOURCE = 2


def make_collector() -> HttpCollector:
    """Collector factory; tests replace it with a fake."""
    return HttpCollector()


def load_creds(path: str | Path | None, env: dict[str, str] | None = None) -> Creds:
    """Credentials from ``TURNUP_*`` environment variables, overridden by ``--creds creds.yaml``."""
    env = dict(os.environ if env is None else env)
    creds = Creds(
        bmc_user=env.get("TURNUP_BMC_USER") or None,
        bmc_pass=env.get("TURNUP_BMC_PASS") or None,
        switch_user=env.get("TURNUP_SWITCH_USER") or env.get("TURNUP_BMC_USER") or None,
        switch_pass=env.get("TURNUP_SWITCH_PASS") or env.get("TURNUP_BMC_PASS") or None,
        snmp_community=env.get("TURNUP_SNMP_COMMUNITY") or "public",
    )
    if not path:
        return creds
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: creds file must be a mapping")
    bmc = doc.get("bmc") or {}
    switch = doc.get("switch") or {}
    snmp = doc.get("snmp") or {}
    if bmc.get("user"):
        creds.bmc_user, creds.bmc_pass = str(bmc["user"]), str(bmc.get("password", ""))
    if switch.get("user"):
        creds.switch_user, creds.switch_pass = str(switch["user"]), str(switch.get("password", ""))
    if snmp.get("community"):
        creds.snmp_community = str(snmp["community"])
    if snmp.get("version"):
        creds.snmp_version = str(snmp["version"])
    for name, entry in (doc.get("devices") or {}).items():
        if isinstance(entry, dict):
            creds.devices[str(name)] = DeviceCreds(
                user=entry.get("user"), password=entry.get("password"), community=entry.get("community")
            )
    return creds


def timeout_error(value: float) -> str | None:
    """Why ``--timeout`` is unusable (NaN, infinite, zero or negative), or ``None`` when it is fine."""
    if not math.isfinite(value) or value <= 0:
        return f"--timeout must be a positive number of seconds, got {value!r}"
    return None


def _check_timeout(_ctx: click.Context, _param: click.Parameter, value: float) -> float:
    if timeout_error(value):
        raise click.BadParameter(f"must be a positive, finite number of seconds (got {value!r})")
    return value


class RunOptions:
    """Everything one run needs; built by the click command or directly by tests."""

    def __init__(
        self,
        *,
        site: str | None,
        intent: str,
        baseline: str | None = None,
        out: str | None = None,
        timeout: float = 3.0,
        json_out: bool = False,
        telemetry: bool = False,
        only: str | None = None,
        creds: str | None = None,
        verbose: bool = False,
        cwd: Path | None = None,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        collector: Collector | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.site = site
        self.intent = intent
        self.baseline = baseline
        self.out = out
        self.timeout = timeout
        self.json_out = json_out
        self.telemetry = telemetry
        self.only = only
        self.creds = creds
        self.verbose = verbose
        self.cwd = cwd or Path.cwd()
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.collector = collector
        self.env = env


async def run(opts: RunOptions) -> int:
    """One check. Returns the exit code."""
    human: TextIO = opts.stderr if opts.json_out else opts.stdout

    err = timeout_error(opts.timeout)
    if err:
        emit([f"{CROSS} {err}"], human)
        return EXIT_NO_SOURCE
    try:
        polled = normalize_only(opts.only)
    except ValueError as exc:
        emit([f"{CROSS} {exc}"], human)
        return EXIT_NO_SOURCE
    try:
        creds = load_creds(opts.creds, opts.env)
    except (OSError, ValueError) as exc:
        emit([f"{CROSS} creds: {exc}"], human)
        return EXIT_NO_SOURCE

    try:
        # --timeout is a per-poll deadline; intent reads get at least INTENT_TIMEOUT because a
        # Nautobot cable list can take a minute server-side.
        intent: Intent = await load_intent(
            opts.intent,
            site=opts.site,
            baseline=opts.baseline,
            timeout=max(opts.timeout, INTENT_TIMEOUT),
            env=opts.env,
        )
    except IntentError as exc:
        emit([f"{'reading intent':<20}failed: {exc}"], human)
        return EXIT_NO_SOURCE
    notes_pre: list[str] = list(intent.notes)
    if opts.site and intent.site != opts.site:
        notes_pre.append(
            f"--site {opts.site} differs from intent site {intent.site}; using the intent's site"
        )

    emit([intent_line(intent), polling_line(polled)], human)

    started = time.monotonic()
    collector = opts.collector
    if collector is None:
        async with make_collector() as http_collector:
            obs, result = await check(intent, http_collector, creds, timeout=opts.timeout, only=polled)
    else:
        obs, result = await check(intent, collector, creds, timeout=opts.timeout, only=polled)
    elapsed = time.monotonic() - started
    result.notes = notes_pre + result.notes

    report = build_report(intent, obs, result, elapsed_s=elapsed)
    report_path = write_report(report, opts.out, cwd=opts.cwd)
    shown_path: str = report_path.name if report_path.parent == opts.cwd else str(report_path)

    telemetry_msg: str | None = None
    if opts.telemetry:
        _ok, telemetry_msg = send_ping(build_ping(report, intent))

    lines = render_lines(report, report_path=shown_path, telemetry=telemetry_msg, include_header=False)
    no_source = bool(polled) and not obs.any_reachable() and (intent.nodes or intent.switches)
    if no_source:
        lines.insert(len(lines) - 3, f"{CROSS} no telemetry source reachable")
    emit(lines, human)
    if opts.verbose and report.notes:
        emit([f"{'note':<13}{n}" for n in report.notes], opts.stderr)

    if opts.json_out:
        opts.stdout.write(report_json(report) + "\n")
        opts.stdout.flush()

    if no_source:
        return EXIT_NO_SOURCE
    return EXIT_FINDINGS if report.counted_findings else EXIT_OK


@click.command(context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100})
@click.option("--site", help="Site (NetBox) or location (Nautobot) slug, e.g. hall-b.")
@click.option(
    "--intent",
    required=True,
    help="netbox | nautobot | <file.yaml|json> | file:<path> | http://host/netbox (a NetBox base URL).",
)
@click.option(
    "--baseline",
    type=click.Path(dir_okay=False),
    help="Firmware baseline YAML: model -> component -> version.",
)
@click.option(
    "--out",
    type=click.Path(dir_okay=False),
    help="Report path (default <site>-<date>.json in the working directory).",
)
@click.option(
    "--timeout",
    type=float,
    default=3.0,
    show_default=True,
    callback=_check_timeout,
    help="Per-request deadline in seconds (positive, finite). NetBox/Nautobot intent reads use at least 120.",
)
@click.option(
    "--json", "json_out", is_flag=True, help="Print the report JSON to stdout (human output goes to stderr)."
)
@click.option(
    "--telemetry", is_flag=True, help="Opt in: send racks checked and links verified to the public counter."
)
@click.option("--only", help="Comma list of sources to poll: lldp,redfish,dcgm.")
@click.option("--creds", type=click.Path(dir_okay=False), help="Credentials YAML (bmc/switch/snmp/devices).")
@click.option(
    "-v", "--verbose", is_flag=True, help="Also print notes (unknowns, unverified links) to stderr."
)
@click.version_option(__version__, prog_name="turnup-check")
def main(**kw: Any) -> None:
    """Diff the intended build (NetBox/Nautobot) against what LLDP, Redfish and DCGM see."""
    opts = RunOptions(**kw)
    code = asyncio.run(run(opts))
    sys.exit(code)


if __name__ == "__main__":  # pragma: no cover
    main()
