"""Pure Studio coffee recipe encoding; no storage, network or machine I/O.

Encoder version identifiers are persisted in plans, so they never change
meaning. The current version encodes RT/BP with the editor model codes and
the reference cup limits. The legacy version is kept only to validate and
repeat plans it produced, byte for byte.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .control import EncodedRecipe
from .recipe_validation import (
    COFFEE_REFERENCE,
    TEA_REFERENCE,
    RecipeValidationError,
    check_recipe,
)

COFFEE_ENCODER_VERSION = "studio-coffee-android-2.2.3-v2"
LEGACY_COFFEE_ENCODER_VERSION = "studio-coffee-android-2.2.3-v1"
# Legacy plans encoded RT/BP with these codes and cup limits.
_LEGACY_TEMPERATURES = {"RT": 39, "BP": 96}
_LEGACY_CUP_LIMITS = {"xpod": (40, 80), "xdripper": (40, 90), "other": (40, 90)}
MAX_POUR_FRAGMENT_ML = 127


def _temperature(value: int | str, mapping: dict[str, int]) -> int:
    return mapping[value] if isinstance(value, str) else value


def encode_coffee_recipe(document: dict[str, Any]) -> EncodedRecipe:
    """Encode a validated coffee document with the current encoder."""
    return _encode_coffee_recipe(document)


def _encode_coffee_recipe_v1(document: dict[str, Any]) -> EncodedRecipe:
    return _encode_coffee_recipe(document, legacy=True)


def _encode_coffee_recipe(
    document: dict[str, Any], *, legacy: bool = False
) -> EncodedRecipe:
    """Validate and encode a complete mode=coffee document, without I/O.

    Payload: one length byte, then for each pour one 4-byte record per volume
    fragment of at most 127 ml (volume, temperature code, pattern code,
    vibration flags) and a 4-byte trailer (negated pause, 0, RPM on the first
    pour only, flow*10); then grind size and ratio*10. Decimal arithmetic keeps
    3.1 and 3.3 ml/s exact.
    """
    report = check_recipe(document)
    if not report["valid"]:
        raise RecipeValidationError(report)
    if report["normalized"]["mode"] != "coffee":
        raise ValueError("encode_coffee_recipe requires mode=coffee")
    content = report["normalized"]["coffee_recipe"]
    pour_codes = (
        _LEGACY_TEMPERATURES if legacy else COFFEE_REFERENCE["pour_temperature_codes"]
    )
    bypass_codes = (
        _LEGACY_TEMPERATURES if legacy else COFFEE_REFERENCE["bypass_temperature_codes"]
    )
    cup_limits = _LEGACY_CUP_LIMITS if legacy else COFFEE_REFERENCE["cup_limits"]
    body = bytearray()
    for index, pour in enumerate(content["pours"]):
        volume = pour["volume_ml"]
        chunks, remainder = divmod(volume, MAX_POUR_FRAGMENT_ML)
        volumes = [MAX_POUR_FRAGMENT_ML] * chunks + (
            [remainder] if remainder or not chunks else []
        )
        flags = int(pour["vibration_before"]) | (int(pour["vibration_after"]) << 1)
        for amount in volumes:
            body.extend(
                (
                    amount,
                    _temperature(pour["temperature"], pour_codes),
                    TEA_REFERENCE["pattern_codes"][pour["pattern"]],
                    flags,
                )
            )
        body.extend(
            (
                (-pour["pause_seconds"]) & 255,
                0,
                content["rpm"] if index == 0 else 0,
                int(Decimal(str(pour["flow_ml_s"])) * 10),
            )
        )
    payload = (
        bytes([len(body)])
        + body
        + bytes([content["grind_size"], int(Decimal(str(content["ratio"])) * 10)])
    )
    bypass = content["bypass"]
    return EncodedRecipe(
        payload,
        content["dose_g"],
        content["grinder_enabled"],
        tuple(cup_limits[content["cup_type"]]),
        bypass["volume_ml"] if bypass else 0,
        _temperature(bypass["temperature"], bypass_codes) if bypass else 20,
    )
