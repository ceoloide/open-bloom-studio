"""Offline, read-only ranking of coffee recipe examples by metadata overlap.

Scores describe metadata overlap, not taste quality or calibrated confidence.
The bundled examples are original, project-authored starting points; pass
``path`` to rank a JSON collection you supply instead (same format as the
bundled ``data/coffee-examples.json``). ``rank_examples`` ranks any records
the caller supplies.
"""

from __future__ import annotations

import json
import math
import os
import re
import unicodedata
from copy import deepcopy
from importlib.resources import files
from typing import Any

from .serialization import read_json_file

FIELDS = ("origin", "region", "type", "variety", "process", "flavor")
WEIGHTS = {"origin": 3, "region": 1, "type": 1, "variety": 2, "process": 4, "flavor": 1}
CONTEXT = ("name", "roaster", "roast")
UNKNOWN = {"", "unknown", "unspecified", "n/a", "none"}
PROCESS_TERMS = (
    ("washed", r"\b(?:washed|wet fermentation)\b"),
    ("natural", r"\bnatural\b"),
    ("honey", r"\bhoney\b"),
    ("anaerobic", r"\banaerobic\b"),
    ("co-ferment", r"\b(?:co[ -]?ferment(?:ation|ed|ing)?|infus(?:ion|ed))\b"),
    ("thermal shock", r"\bthermal shock\b"),
    ("decaf", r"\b(?:decaf|ethyl acetate)\b"),
    ("extended", r"\b(?:extended|double fermentation|dry ferment(?:ation)?)\b"),
    ("carbonic maceration", r"\b(?:carbonic maceration|cm)\b"),
    ("yeast", r"\byeast(?: inoculation)?\b"),
    ("bioreactor", r"\bbio[ -]?reactor\b"),
)
LIMITATIONS = [
    "Metadata overlap is not a taste prediction; roast does not affect ranking.",
    "Examples are starting points for a 15 g dose; other doses and cups are "
    "extrapolations.",
    "Example settings can violate current authoring constraints. Validate any draft.",
    "Flow, vibration and grinder enablement are not part of the examples.",
    "Similar examples are not independent evidence. No settings are scaled or saved.",
]


def text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    return " ".join("".join(c for c in value if not unicodedata.combining(c)).split())


def tokens(value: str | list[str], field: str) -> set[str]:
    """Normalize categorical sets; keep modifiers on processing methods."""
    values = value if isinstance(value, list) else [value]
    result: set[str] = set()
    for item in values:
        normalized = text(item)
        if normalized in UNKNOWN:
            continue
        if field == "process":
            residual = normalized
            for term, pattern in PROCESS_TERMS:
                if re.search(pattern, normalized):
                    result.add(term)
                    residual = re.sub(pattern, " ", residual)
            residual = re.sub(
                r"\b(?:fully|classic|process(?:ed|ing)?|with|and|the|of)\b",
                " ",
                residual,
            )
            residual = " ".join(re.sub(r"[^\w]+", " ", residual).split())
            if residual:
                # Unknown processing details are evidence, not disposable prose.
                result.add(residual)
        else:
            for part in re.split(r"[,;·・•&]+", normalized):
                part = part.strip()
                if field == "variety":
                    part = re.sub(r"\bsl[ -]?(\d+)\b", r"sl\1", part)
                    part = part.replace("gesha", "geisha")
                    if re.search(r"heirloom|landrace", part):
                        part = "heirloom/landrace"
                if part and part not in UNKNOWN:
                    result.add(part)
    return result


def _check_corpus(corpus: Any) -> dict[str, Any]:
    if not isinstance(corpus, dict) or not isinstance(corpus.get("records"), list):
        raise ValueError("Coffee examples must be an object with a records list")
    for record in corpus["records"]:
        if (
            not isinstance(record, dict)
            or isinstance(record.get("id"), bool)
            or not isinstance(record.get("id"), (int, str))
            or not isinstance(record.get("coffee"), dict)
            or not isinstance(record.get("brew"), dict)
            or any(not isinstance(v, (str, list)) for v in record["coffee"].values())
        ):
            raise ValueError(
                "Each coffee example needs an id, a coffee object of strings and "
                "a brew object"
            )
    return corpus


def load_corpus(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Return a fresh copy of the bundled examples, or of a supplied file."""
    if path is None:
        resource = files("xbloom_studio").joinpath("data/coffee-examples.json")
        return _check_corpus(json.loads(resource.read_text(encoding="utf-8")))
    return _check_corpus(read_json_file(path))


def _order(record: dict[str, Any]) -> tuple[bool, Any]:
    ident = record["id"]
    return (isinstance(ident, str), ident)


def rank_examples(
    records: list[dict[str, Any]],
    coffee: dict[str, Any],
    water_ml: float | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Rank only the supplied records; no I/O, saving, scaling or recipe creation."""
    if not isinstance(coffee, dict) or set(coffee) - set(FIELDS + CONTEXT):
        raise ValueError(
            "coffee must contain only origin, region, type, variety, process, "
            "flavor, name, roaster or roast"
        )
    for value in coffee.values():
        if not isinstance(value, (str, list)) or (
            isinstance(value, list) and any(not isinstance(v, str) for v in value)
        ):
            raise ValueError("coffee values must be strings or lists of strings")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("limit must be an integer in 1..20")
    if water_ml is not None and (
        isinstance(water_ml, bool)
        or not isinstance(water_ml, (int, float))
        or not math.isfinite(water_ml)
        or not 1 <= water_ml <= 1000
    ):
        raise ValueError("water_ml must be a number in 1..1000")
    query = {field: tokens(coffee.get(field, ""), field) for field in FIELDS}
    if not any(query.values()):
        return []
    ranked: list[dict[str, Any]] = []
    for record in records:
        if water_ml is not None and record["brew"].get("water_ml") != water_ml:
            continue
        matches: dict[str, list[str]] = {}
        conflicts: dict[str, dict[str, list[str]]] = {}
        unknown: list[str] = []
        score = 0.0
        for field in FIELDS:
            q = query[field]
            if not q:
                continue
            candidate = tokens(record["coffee"].get(field, ""), field)
            if not candidate:
                unknown.append(field)
                continue
            common = q & candidate
            if common:
                matches[field] = sorted(common)
                score += WEIGHTS[field] * len(common) / len(q | candidate)
            # Distinguish partial blend/variety overlap from exact equality.
            if q != candidate:
                conflicts[field] = {
                    "requested_only": sorted(q - candidate),
                    "example_only": sorted(candidate - q),
                }
                if field == "process":
                    # Extra advanced processing is a material mismatch even when
                    # both coffees share a washed or natural base process.
                    score -= WEIGHTS[field] * len(q ^ candidate) / len(q | candidate)
                elif not common:
                    score -= WEIGHTS[field]
        if matches:
            ranked.append(
                {
                    "example": deepcopy(record),
                    "score": round(score, 6),
                    "matched": matches,
                    "conflicting": conflicts,
                    "unknown": unknown,
                }
            )
    ranked.sort(key=lambda r: (-r["score"], *_order(r["example"])))
    return ranked[:limit]


def recipe_examples(
    coffee: dict[str, Any],
    water_ml: float | None = None,
    limit: int = 5,
    path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Rank bundled or supplied examples; never claim they are executable drafts."""
    corpus = load_corpus(path)
    return {
        "corpus_version": corpus.get("version"),
        "snapshot_date": corpus.get("snapshot_date"),
        "scope": corpus.get("scope"),
        "source": "bundled" if path is None else "user_supplied",
        "examples": rank_examples(corpus["records"], coffee, water_ml, limit),
        "limitations": list(LIMITATIONS),
    }
