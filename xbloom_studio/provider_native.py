"""Explicit structured source-to-recipe conversion. No network, storage or BLE.

Source enums map cup 1/2/3/4 to xPod/Omni (xdripper)/other/tea, pattern
1/2/3 to center/spiral/ring and flags 1/2 to true/false. ``grandWater`` is
the ratio, not a volume. Unknown fields, missing settings and other machines
are refused rather than guessed; the source is retained by the caller.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .recipes import compile_recipe, validate_recipe

KNOWN_FIELDS = frozenset(
    {
        "adaptedModel", "bypassTemp", "bypassVolume", "createTimeStamp", "cupType",
        "cupTypeName", "dose", "grandWater", "grinderSize", "isDefault",
        "isEnableBypassWater", "isSetGrinderSize", "isShortcuts", "podsVo",
        "pourCount", "pourList", "resourceRegion", "resourceTableId", "rpm",
        "shareRecipeLink", "subSetType", "tableId", "theColor", "theName",
        "theSubsetId", "recipeName", "name", "model", "id", "recipeId", "author",
        "description", "lastSyncedAt", "ratio", "shareMemberHead",
        "shareMemberName", "totalBrewWater",
    }
)  # fmt: skip
POUR_FIELDS = frozenset(
    {
        "flowRate", "isEnableVibrationAfter", "isEnableVibrationBefore", "pattern",
        "pausing", "recipeId", "tableId", "temperature", "theName", "volume",
    }
)  # fmt: skip
OPTIONAL_POUR_FIELDS = frozenset({"recipeId", "tableId", "theName"})
CUPS = {1: "xpod", 2: "xdripper", 3: "other", 4: "tea"}
PATTERNS = {1: "center", 2: "spiral", 3: "ring"}
# Model temperature codes for RT and BP; the coffee bypass BP code differs.
TEA_CODES = {39: "RT", 96: "BP"}
COFFEE_CODES = {20: "RT", 98: "BP"}
BYPASS_CODES = {20: "RT", 96: "BP"}


def _numeric(value: object) -> bool:
    return type(value) in (int, float)


def native_document(
    source: dict[str, Any], target: str, error: type[Exception]
) -> dict[str, Any]:
    """Convert a structured coffee or tea source and prove it compiles."""
    raw = source.get("native_response", source.get("raw"))
    if not isinstance(raw, dict):
        raise error("Native source must be an object")
    recipe = raw.get("recipeVo", raw.get("xBloomData", raw))
    if not isinstance(recipe, dict):
        raise error("Native recipe must be an object")
    if set(recipe) - KNOWN_FIELDS:
        unknown = ", ".join(sorted(set(recipe) - KNOWN_FIELDS))
        raise error("Unknown native recipe fields: " + unknown)

    def required(key: str) -> Any:
        if key not in recipe:
            raise error("Native source is missing " + key)
        return recipe[key]

    def flag(value: Any, label: str) -> bool:
        if not _numeric(value) or value not in (1, 2):
            raise error(label + " must be source enum 1 or 2")
        return bool(value == 1)

    def integer(value: Any, label: str) -> int:
        if type(value) is int:
            return int(value)
        if type(value) is not float or not value.is_integer():
            raise error(label + " must be a whole number")
        return int(value)

    if not _numeric(recipe.get("adaptedModel")) or recipe["adaptedModel"] != 1:
        raise error("Native source must explicitly target Studio (adaptedModel=1)")
    cup = required("cupType")
    if not _numeric(cup) or cup not in CUPS:
        raise error("Unknown native cup type")
    if (target == "tea") != (cup == 4):
        raise error("Conversion target does not match source coffee/tea cup type")
    pours = required("pourList")
    if not isinstance(pours, list) or not pours:
        raise error("Native source has no pours/steeps")
    if recipe.get("pourCount") is not None and (
        not _numeric(recipe["pourCount"]) or recipe["pourCount"] != len(pours)
    ):
        raise error("Source pourCount disagrees with pourList")
    set_size = flag(required("isSetGrinderSize"), "isSetGrinderSize")
    bypass_enabled = flag(required("isEnableBypassWater"), "isEnableBypassWater")
    if recipe.get("ratio") is not None and (
        not _numeric(recipe["ratio"]) or recipe["ratio"] != required("grandWater")
    ):
        raise error("Source ratio disagrees with grandWater")
    converted = []
    for pour in pours:
        if not isinstance(pour, dict):
            raise error("Native pour must be an object")
        if set(pour) - POUR_FIELDS:
            raise error("Unknown native pour fields")
        missing = POUR_FIELDS - OPTIONAL_POUR_FIELDS - set(pour)
        if missing:
            raise error("Native pour is missing " + ", ".join(sorted(missing)))
        pattern = pour["pattern"]
        if not _numeric(pattern) or pattern not in PATTERNS:
            raise error("Unknown native source pour pattern")
        before = flag(pour["isEnableVibrationBefore"], "isEnableVibrationBefore")
        after = flag(pour["isEnableVibrationAfter"], "isEnableVibrationAfter")
        temperature = integer(pour["temperature"], "temperature")
        codes = TEA_CODES if target == "tea" else COFFEE_CODES
        item: dict[str, Any] = {
            "volume_ml": integer(pour["volume"], "volume"),
            "temperature": codes.get(temperature, temperature),
            "pattern": PATTERNS[pattern],
        }
        if target == "tea":
            item.update(
                steep_seconds=integer(pour["pausing"], "pausing"),
                flow_ml_s=pour["flowRate"],
                vibration_before=before,
                vibration_after=after,
            )
        else:
            item.update(
                flow_ml_s=pour["flowRate"],
                pause_seconds=integer(pour["pausing"], "pausing"),
                vibration_before=before,
                vibration_after=after,
            )
        converted.append(item)
    if recipe.get("totalBrewWater") is not None and (
        not _numeric(recipe["totalBrewWater"])
        or recipe["totalBrewWater"] != sum(p["volume_ml"] for p in converted)
    ):
        raise error(
            "Source totalBrewWater disagrees with pour volumes; explicit "
            "interpretation required"
        )
    name = (
        recipe.get("theName")
        or recipe.get("recipeName")
        or recipe.get("name")
        or raw.get("name")
    )
    if not isinstance(name, str) or not name.strip():
        raise error("Native source is missing a recipe name")
    document: dict[str, Any] = {
        "name": name,
        "mode": target,
        "coffee": deepcopy(recipe.get("podsVo") or {}),
        "source": {
            **deepcopy(source),
            "conversion_target": target,
            "adaptation": "structured source mapping; reference cup limits; "
            "physical equivalence unverified",
        },
    }
    if target == "tea":
        if set_size or bypass_enabled:
            raise error(
                "Dedicated tea upload does not represent grinder-setting upload or "
                "bypass; raw source retained"
            )
        document["tea"] = {
            "dose_g": required("dose"),
            "steeps": converted,
            "grinder_size_metadata": integer(required("grinderSize"), "grinderSize"),
            "rpm_metadata": integer(required("rpm"), "rpm"),
            "ratio_metadata": required("grandWater"),
        }
    else:
        bypass = None
        if bypass_enabled:
            bypass_temperature = integer(required("bypassTemp"), "bypassTemp")
            bypass = {
                "volume_ml": integer(required("bypassVolume"), "bypassVolume"),
                "temperature": BYPASS_CODES.get(bypass_temperature, bypass_temperature),
            }
        document["coffee_recipe"] = {
            "cup_type": CUPS[cup],
            "dose_g": integer(required("dose"), "dose"),
            "grind_size": integer(required("grinderSize"), "grinderSize"),
            "rpm": integer(required("rpm"), "rpm"),
            "ratio": required("grandWater"),
            "set_grinder_size": set_size,
            "bypass": bypass,
            "pours": converted,
        }
    document = validate_recipe(document)
    compile_recipe(document)
    return document
