# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Report writer (``turnup.report/v1``) and the exact CLI layout of CONTRACT.md section 4."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from turnup_check.diff import DiffResult
from turnup_check.models import Finding, Intent, Observations, Report, iso_z, utcnow

CHECK = chr(0x2713)  # check mark
CROSS = chr(0x2717)  # ballot x
DOT = chr(0xB7)  # middle dot
ARROW = chr(0x2192)  # rightwards arrow
ASCII = {CHECK: "ok", CROSS: "!!", DOT: "-", ARROW: "->"}

LABEL_WIDE = 20  # "reading intent", "polling"
LABEL = 13  # everything else

CLASS_LABEL: dict[str, str] = {
    "wrong_port": "mismatch",
    "missing_link": "missing",
    "unexpected_link": "unexpected",
    "link_degraded": "degraded",
    "unreachable_node": "offline",  # 11-character "unreachable" would overflow the 13-column label
    "firmware_off_baseline": "firmware",
    "gpu_count_mismatch": "gpu count",
    "gpu_unhealthy": "gpu",
    "nvlink_degraded": "nvlink",
    "node_health_warning": "health",
}


def report_filename(site: str, when: datetime | None = None) -> str:
    """``<site>-<YYYY-MM-DD>.json`` with the UTC date."""
    when = when or utcnow()
    return f"{site}-{iso_z(when)[:10]}.json"


def build_report(
    intent: Intent,
    obs: Observations,
    result: DiffResult,
    *,
    elapsed_s: float,
    generated_at: datetime | None = None,
) -> Report:
    generated_at = generated_at or utcnow()
    return Report(
        site=intent.site,
        simulated=intent.simulated,
        generated_at=iso_z(generated_at),
        elapsed_s=round(elapsed_s, 3),
        intent=intent.summary(),
        summary=result.summary,
        findings=result.findings,
        sources=obs.sources(),
        notes=list(result.notes),
    )


def write_report(report: Report, out: str | Path | None = None, *, cwd: Path | None = None) -> Path:
    """Write the JSON report; ``out`` overrides the default ``<site>-<date>.json`` in ``cwd``."""
    if out:
        path = Path(out)
    else:
        path = (cwd or Path.cwd()) / report_filename(
            report.site, datetime.fromisoformat(report.generated_at.replace("Z", "+00:00"))
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_json(), indent=1) + "\n", encoding="utf-8")
    return path


def report_json(report: Report) -> str:
    return json.dumps(report.as_json(), indent=1)


# --------------------------------------------------------------------------------------
# CLI layout
# --------------------------------------------------------------------------------------


def _row(label: str, text: str, width: int = LABEL) -> str:
    return f"{label:<{width}}{text}"


def intent_line(intent: Intent) -> str:
    s = intent.summary()
    return _row(
        "reading intent",
        f"{s['links']} links {DOT} {s['nodes']} nodes {DOT} {s['leaves']} leaves",
        LABEL_WIDE,
    )


def polling_line(polled: list[str]) -> str:
    return _row("polling", f" {DOT} ".join(polled), LABEL_WIDE)


def status_lines(report: Report) -> list[str]:
    """The two summary lines. The check mark marks a summary; the cross marks a finding block.

    CONTRACT section 4 and the README example print ``✓ 287 / 288 links match intent`` above the
    ``✗ mismatch`` block, so the summary glyph never changes with the numbers.
    """
    s = report.summary
    lines: list[str] = []
    polled = {src["kind"] for src in report.sources}
    if "lldp" in polled:
        lines.append(f"{CHECK} {s.links_ok} / {s.links_total} links match intent")
    if "redfish" in polled:
        if not s.firmware_ok:
            fw = "firmware off baseline"
        elif not s.firmware_compared:
            fw = "firmware not compared"  # no baseline for the models seen; "ok" would be a claim
        else:
            fw = "firmware baseline ok"
        lines.append(f"{CHECK} {s.nodes_reachable} / {s.nodes_total} nodes reachable, {fw}")
    return lines


def _headline(f: Finding) -> tuple[str, str | None]:
    """(headline, detail) for a finding block."""
    dev = f.device or "?"
    itf = f.interface
    if f.class_ == "wrong_port":
        return f"{dev} {itf} {ARROW} {f.observed}", f"expected {f.expected}"
    if f.class_ == "missing_link":
        return f"{dev} {itf} {ARROW} not seen", f"expected {f.expected}"
    if f.class_ == "link_degraded":
        return f"{dev} {itf} {ARROW} {f.observed}", f"expected {f.expected}"
    if f.class_ == "unexpected_link":
        return f"{dev}/{itf} shows {f.observed}" if itf else f"{dev} shows {f.observed}", None
    if f.class_ == "unreachable_node":
        return f"{dev} {f.diagnosis or f.observed}", None
    if f.class_ == "firmware_off_baseline":
        return f"{dev} {itf} {f.observed}", f"expected {f.expected}"
    if f.class_ == "gpu_count_mismatch":
        return f"{dev} {f.observed} GPUs", f"expected {f.expected}"
    if f.class_ == "gpu_unhealthy":
        return f"{dev} {itf} {f.observed}", f"expected {f.expected}"
    if f.class_ == "nvlink_degraded":
        return f"{dev} nvlink degraded", f.diagnosis
    if f.class_ == "node_health_warning":
        return f"{dev} health {f.observed}", f"expected {f.expected}"
    return f"{dev} {f.observed}", f.diagnosis


def finding_lines(f: Finding) -> list[str]:
    mark = DOT if f.severity == "info" else CROSS
    label = CLASS_LABEL.get(f.class_, f.class_)
    headline, detail = _headline(f)
    lines = [f"{mark} {label:<{LABEL - 2}}{headline}"]
    if detail:
        lines.append(" " * LABEL + detail)
    if f.fix:
        lines.append(_row("fix", f.fix))
    return lines


def tail_lines(
    report: Report, report_path: Path | str | None, telemetry: str | None, elapsed_s: float
) -> list[str]:
    lines = [_row("elapsed", f"{elapsed_s:.1f} s"), ""]
    if report_path is not None:
        lines.append(_row("report", str(report_path)))
    lines.append(_row("share stats", telemetry or "--telemetry opt-in (racks, links)"))
    return lines


def render_lines(
    report: Report,
    *,
    report_path: Path | str | None,
    telemetry: str | None = None,
    include_header: bool = True,
    intent: Intent | None = None,
) -> list[str]:
    """Every line of the CLI output after (optionally) the two header lines."""
    lines: list[str] = []
    if include_header and intent is not None:
        lines.append(intent_line(intent))
        lines.append(
            polling_line(
                [src["kind"] for src in report.sources if src["kind"] in ("lldp", "redfish", "dcgm")]
            )
        )
    lines.extend(status_lines(report))
    for f in report.findings:
        lines.extend(finding_lines(f))
    lines.extend(tail_lines(report, report_path, telemetry, report.elapsed_s))
    return lines


def to_ascii(line: str) -> str:
    for glyph, repl in ASCII.items():
        line = line.replace(glyph, repl)
    return line


def can_encode(stream: TextIO, text: str) -> bool:
    enc = getattr(stream, "encoding", None) or "ascii"
    try:
        text.encode(enc)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def emit(lines: list[str], stream: TextIO | None = None, *, color: bool | None = None) -> None:
    """Print lines: rich on a TTY, plain text otherwise, ASCII when the stream cannot encode the glyphs."""
    stream = stream or sys.stdout
    sample = CHECK + CROSS + DOT + ARROW
    if not can_encode(stream, sample):
        lines = [to_ascii(line) for line in lines]
    use_color = stream.isatty() if color is None else color
    if not use_color:
        for line in lines:
            stream.write(line + "\n")
        stream.flush()
        return
    try:
        from rich.console import Console
        from rich.text import Text
    except ImportError:  # pragma: no cover - rich is a hard dependency
        for line in lines:
            stream.write(line + "\n")
        return
    console = Console(file=stream, highlight=False, markup=False, emoji=False, soft_wrap=True)
    for line in lines:
        text = Text(line)
        if line.startswith((CHECK, "ok")):
            text.stylize("bold green", 0, 2)
        elif line.startswith((CROSS, "!!")):
            text.stylize("bold red", 0, 2)
        elif line.startswith("fix"):
            text.stylize("bold", 0, 3)
        elif line[:1].isalpha():
            text.stylize(
                "dim", 0, min(len(line), LABEL_WIDE if line.startswith(("reading", "polling")) else LABEL)
            )
        console.print(text)


def finding_table(report: Report) -> list[dict[str, Any]]:
    """Flat rows for other front ends (the NetBox plugin, acceptance)."""
    return [f.as_json() for f in report.findings]


__all__ = [
    "ARROW",
    "CHECK",
    "CLASS_LABEL",
    "CROSS",
    "DOT",
    "build_report",
    "emit",
    "finding_lines",
    "intent_line",
    "polling_line",
    "render_lines",
    "report_filename",
    "report_json",
    "status_lines",
    "tail_lines",
    "to_ascii",
    "write_report",
]
