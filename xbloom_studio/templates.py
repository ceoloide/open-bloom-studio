"""Bundled starter recipes; never seed a database or configure a machine."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any


def recipe_templates() -> list[dict[str, Any]]:
    """Return detached template envelopes with stable IDs and provenance.

    The bundled templates are original, project-authored starting points.
    Save or edit an envelope's document explicitly with the recipe APIs.
    """
    directory = files("xbloom_studio").joinpath("data/recipes")
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.iterdir(), key=lambda p: p.name)
        if path.name.endswith(".json")
    ]
