# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""File intent: a ``turnup.intent/v1`` document in YAML or JSON, and ``--baseline`` files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from turnup_check.models import Intent, version_text


class YamlFloat(float):
    """A YAML float that keeps its source text: ``bmc: 1.10`` is 1.1 as a number, "1.10" as a version."""

    yaml_text: str


class YamlInt(int):
    """A YAML int that remembers its source text (``bios: 010`` is 8 in YAML 1.1, ``"010"`` as a version)."""

    yaml_text: str


class _TextKeepingLoader(yaml.SafeLoader):
    """SafeLoader whose numbers carry their scalar text, so version strings survive unquoted."""


def _float_with_text(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> float:
    value = YamlFloat(loader.construct_yaml_float(node))
    value.yaml_text = str(node.value)
    return value


def _int_with_text(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> int:
    value = YamlInt(loader.construct_yaml_int(node))
    value.yaml_text = str(node.value)
    return value


_TextKeepingLoader.add_constructor("tag:yaml.org,2002:float", _float_with_text)
_TextKeepingLoader.add_constructor("tag:yaml.org,2002:int", _int_with_text)


def _read_document(path: str | Path) -> Any:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.load(text, Loader=_TextKeepingLoader)  # a SafeLoader subclass


def load_intent_file(path: str | Path) -> Intent:
    """Parse a YAML or JSON intent file. Raises ``ValueError`` on a malformed document."""
    try:
        doc = _read_document(path)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: not valid YAML/JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(
            f"{path}: intent must be a mapping with schema {Intent.model_fields['schema_'].default}"
        )
    try:
        intent = Intent.model_validate(doc)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(x) for x in first.get("loc", ()))
        raise ValueError(f"{path}: {loc}: {first.get('msg', exc)}") from exc
    intent.source = "file"
    return intent


def merge_baseline_file(intent: Intent, path: str | Path) -> None:
    """Apply ``--baseline baseline.yaml`` (``model -> component -> version``) under the intent's own baseline.

    Baselines carried by the intent (for example NetBox config context) win over the file.
    """
    try:
        doc = _read_document(path)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: not valid YAML/JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: baseline must map model -> component -> version")
    # Accept either {model: {component: version}} or {"firmware_baseline": {...}}.
    if "firmware_baseline" in doc and isinstance(doc["firmware_baseline"], dict):
        doc = doc["firmware_baseline"]
    merged: dict[str, dict[str, str]] = {}
    for model, comps in doc.items():
        if not isinstance(comps, dict):
            raise ValueError(f"{path}: baseline for {model!r} must be a mapping of component -> version")
        merged[str(model)] = {str(k).lower(): version_text(v) for k, v in comps.items()}
    for model, comps in intent.firmware_baseline.items():
        merged.setdefault(model, {}).update(comps)
    intent.firmware_baseline = merged
