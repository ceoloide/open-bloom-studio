"""Offline tea reference profiles. Selection and storage belong to callers.

The bundled profiles are original, project-authored starting points, one per
family. Pass ``path`` to use a JSON collection you supply instead (same format
as the bundled ``data/tea-references.json``), for example with published
source records alongside your own profiles.
"""

from __future__ import annotations

import json
import os
from importlib.resources import files
from typing import Any

from .recipe_validation import recipe_constraints
from .serialization import read_json_file

FAMILIES = ("green", "black", "oolong", "white", "pu-erh", "herbal", "cascara")


def _check_corpus(corpus: Any) -> dict[str, Any]:
    if (
        not isinstance(corpus, dict)
        or not isinstance(corpus.get("records", []), list)
        or not isinstance(corpus.get("profiles"), list)
    ):
        raise ValueError("Tea references must be an object with a profiles list")
    for item in [*corpus.get("records", []), *corpus["profiles"]]:
        if not isinstance(item, dict) or item.get("family") not in FAMILIES:
            raise ValueError(
                "Every tea record and profile needs a family: " + ", ".join(FAMILIES)
            )
    for profile in corpus["profiles"]:
        tea = profile.get("document", {}).get("tea")
        if not isinstance(tea, dict) or not isinstance(tea.get("steeps"), list):
            raise ValueError("Every tea profile needs a tea recipe document")
    return corpus


def load_tea_corpus(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Return a fresh copy of the bundled references, or of a supplied file."""
    if path is None:
        resource = files("xbloom_studio").joinpath("data/tea-references.json")
        return _check_corpus(json.loads(resource.read_text(encoding="utf-8")))
    return _check_corpus(read_json_file(path))


def _ranges(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize coherent profiles, not a Cartesian space of valid combinations."""
    values: dict[str, list[Any]] = {
        "dose_g": [p["dose_g"] for p in profiles],
        "soak_volume_ml": [v for p in profiles for v in p["soak_volumes_ml"]],
        "temperature_c": [t for p in profiles for t in p["temperatures_c"]],
        "steep_seconds": [t for p in profiles for t in p["steep_seconds"]],
    }
    return {
        key: {"minimum": min(numbers), "maximum": max(numbers)}
        for key, items in values.items()
        if (numbers := [v for v in items if not isinstance(v, str)])
    }


def _summary(document: dict[str, Any]) -> dict[str, Any]:
    steeps = document["tea"]["steeps"]
    return {
        "dose_g": document["tea"]["dose_g"],
        "soak_volumes_ml": [s["volume_ml"] for s in steeps],
        "temperatures_c": [s["temperature"] for s in steeps],
        "steep_seconds": [s["steep_seconds"] for s in steeps],
    }


def recipe_tea_references(
    family: str | None = None, path: str | os.PathLike[str] | None = None
) -> dict[str, Any]:
    """Get coherent ~240 ml drafts, any published ranges and authoring bounds.

    Families are green/black/oolong/white/pu-erh/herbal/cascara; unknown keys
    fail explicitly. Profiles are starting points, not taste claims. RT/BP
    temperatures are listed but excluded from numeric ranges. The lookup
    neither saves nor connects, and each result is detached.
    """
    if family is not None and (not isinstance(family, str) or family not in FAMILIES):
        raise ValueError(f"family must be one of {', '.join(FAMILIES)}, or None")
    corpus = load_tea_corpus(path)
    selected = [family] if family is not None else list(FAMILIES)
    result = []
    for key in selected:
        records = [r for r in corpus.get("records", []) if r["family"] == key]
        profiles = [p for p in corpus["profiles"] if p["family"] == key]
        published = [
            {
                **r["published"],
                "temperatures_c": r["published"]["temperatures_c_as_published"],
            }
            for r in records
        ]
        drafts = [_summary(p["document"]) for p in profiles]
        result.append(
            {
                "family": key,
                "source_record_count": len(records),
                "source_ids": [r.get("source_id") for r in records],
                "published_ranges": {
                    "record_count": len(records),
                    "ranges": _ranges(published),
                },
                "adapted_draft_ranges": {
                    "profile_count": len(profiles),
                    "ranges": _ranges(drafts),
                },
                "profiles": profiles,
            }
        )
    constraints = recipe_constraints()
    return {
        "version": corpus.get("version"),
        "snapshot_date": corpus.get("snapshot_date"),
        "scope": corpus.get("scope"),
        "source": "bundled" if path is None else "user_supplied",
        "available_families": list(FAMILIES),
        "families": result,
        "nominal_output_ml": 240,
        "yield_verified": False,
        "authoring_bounds": constraints["tea"],
        "tea_reference": constraints["tea_reference"],
        "limitations": list(corpus.get("limitations", [])),
    }
