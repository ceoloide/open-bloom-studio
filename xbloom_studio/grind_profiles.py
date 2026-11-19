"""Grinder profiles are ordinary validated host recipes, with no dose inference."""

from __future__ import annotations

from typing import Any

from .recipes import validate_recipe


def grind_profile(name: str, size: int, rpm: int, duration_s: float) -> dict[str, Any]:
    """Return an unsaved recipe from explicit user-owned settings, without I/O.

    Save, update or alias it with the recipe API. Measured output belongs in
    brew feedback, never an assumed conversion from grams to seconds.
    """
    return validate_recipe(
        {
            "name": name,
            "mode": "host",
            "steps": [
                {"kind": "grind", "size": size, "rpm": rpm, "duration_s": duration_s}
            ],
        }
    )
