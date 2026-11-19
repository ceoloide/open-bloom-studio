"""Named incoming events. Settings and raw codes are not physical sensors."""

from __future__ import annotations

from typing import Any

from .protocol import Packet

ERROR_COMMAND = 65534
TRANSPORT_ERROR_COMMAND = 65533
RESPONSE_ERROR_FLOOR = 50001
# Responses above the error floor that are not errors.
RESPONSE_ERROR_EXCEPTIONS = (50038, 50039)
DECODED = "decoded"


def protocol_error(packet: Packet) -> dict[str, Any] | None:
    """Decode error envelopes without inventing a rejected-command identifier."""
    if packet.device != 2:
        return None
    if packet.command == ERROR_COMMAND and packet.marker == 0xCD:
        kind = "machine_error"
    elif (
        packet.command == ERROR_COMMAND
        and packet.marker == 0xC1
        and packet.function != 7
    ):
        kind = "protocol_error"
    elif packet.command == TRANSPORT_ERROR_COMMAND and packet.marker == 0xCD:
        kind = "transport_error"
    elif (
        (packet.function, packet.marker) == (7, 0xC1)
        and packet.command > RESPONSE_ERROR_FLOOR
        and packet.command not in RESPONSE_ERROR_EXCEPTIONS
    ):
        kind = "command_response_error"
    else:
        return None
    value = (
        int.from_bytes(packet.payload[:4], "little")
        if len(packet.payload) >= 4
        else None
    )
    return {
        "kind": kind,
        "command": packet.command,
        "function": packet.function,
        "marker": packet.marker,
        "payload_hex": packet.payload.hex(),
        "value": value,
        "malformed": kind == "machine_error" and value is None,
        "evidence": DECODED,
    }


SCALARS = {
    8105: ("grind_setting", "grinder_setting_changed"),
    8106: ("grinder_rpm_setting", "grinder_rpm_changed"),
    8107: ("brewer_mode_code", "brewer_mode_changed"),
    8108: ("brewer_temperature_setting_raw", "brewer_temperature_changed"),
    8111: ("selected_preset_index", "preset_selected"),
    8113: ("tea_soak_time_raw", "tea_soak_time_changed"),
    40505: ("grinder_position_progress", "grinder_position_progress"),
    40510: ("pour_index", "pour_stage"),
    40526: ("grinder_position", "grinder_position"),
}
# Scalars carried in all payload bytes rather than a uint32 prefix.
WHOLE_PAYLOAD_SCALARS = (8111, 8113)
GRIND_SETTING_OFFSET = 30
SIGNALS = {
    9002: "scale_entered",
    9004: "grinder_exited",
    9006: "brewer_exited",
    9008: "scale_exited",
    9009: "grinder_paused",
    9010: "brewer_paused",
    9011: "tea_restarted",
    9012: "tea_soaking",
    40502: "recipe_brewer_started",
    40507: "grinder_stopped",
    40511: "brewer_stopped",
    40512: "recipe_enjoy",
    40513: "recipe_enjoy_alternate",
    40515: "tea_paused",
    40520: "bypass_started",
    40527: "before_vibration",
    8203: "abnormal_grinder_position",
    8204: "abnormal_dose_or_water",
    40517: "idling_fault",
}


def _size(payload: bytes, minimum: int) -> None:
    if len(payload) < minimum:
        raise ValueError(f"Event needs at least {minimum} payload bytes")


def _uint32s(payload: bytes, *names: str) -> dict[str, int]:
    return {
        name: int.from_bytes(payload[4 * index : 4 * index + 4], "little")
        for index, name in enumerate(names)
    }


def decode_event(packet: Packet) -> dict[str, Any] | None:
    """Return a named event with raw payload retained, or None for unmodeled frames.

    Minimum lengths are prefix reads; trailing firmware extensions remain
    visible. Signal events are observations, never inferred durable states.
    """
    error = protocol_error(packet)
    if error is not None:
        return {
            "name": "protocol_error",
            "data": error,
            "command": packet.command,
            "payload_hex": packet.payload.hex(),
            "evidence": DECODED,
        }
    if (packet.device, packet.function, packet.marker) != (2, 7, 0xC1):
        return None
    code, payload = packet.command, packet.payload
    data: dict[str, Any]
    if code == 40526 and not payload:
        return None  # Empty housekeeping ACK shares the position event's ID.
    if code in SCALARS:
        whole = code in WHOLE_PAYLOAD_SCALARS
        _size(payload, 1 if whole else 4)
        field, name = SCALARS[code]
        value = int.from_bytes(payload if whole else payload[:4], "little")
        data = {field: value - GRIND_SETTING_OFFSET if code == 8105 else value}
    elif code in SIGNALS:
        name, data = SIGNALS[code], {}
    elif code == 8015:
        _size(payload, 12)
        name = "units_source_changed"
        data = dict(
            _uint32s(
                payload,
                "weight_unit_code",
                "temperature_unit_code",
                "water_source_code",
            )
        )
    elif code in (9000, 9003):
        _size(payload, 8)
        name = "grinder_entered" if code == 9000 else "grinder_started"
        data = dict(
            _uint32s(payload, "grinder_configuration_size_raw", "grinder_rpm_setting")
        )
    elif code in (9001, 9005):
        _size(payload, 12)
        name = "brewer_entered" if code == 9001 else "brewer_started"
        data = dict(
            _uint32s(
                payload,
                "brewer_volume_setting_raw",
                "brewer_temperature_setting_raw",
                "pour_pattern_code",
            )
        )
        if len(payload) >= 16:
            data["brewer_temperature_f_setting_raw"] = int.from_bytes(
                payload[12:16], "little"
            )
    elif code == 40501:
        _size(payload, 6)
        name = "pod_detected"
        try:
            data = {"pod_id": payload[:6].decode("ascii")}
        except UnicodeDecodeError as exc:
            raise ValueError("Pod identifier is not ASCII") from exc
    elif code == 11518:
        _size(payload, 4)
        name, data = "preset_state", {"preset_state_hex": payload[:4].hex()}
    else:
        return None
    return {
        "name": name,
        "data": data,
        "command": code,
        "payload_hex": payload.hex(),
        "evidence": DECODED,
    }


# Public field names shared by telemetry decoding and read-intent validation.
EVENT_FIELDS = tuple(
    dict.fromkeys(
        [field for field, _ in SCALARS.values()]
        + [
            "weight_unit_code",
            "temperature_unit_code",
            "water_source_code",
            "grinder_configuration_size_raw",
            "brewer_volume_setting_raw",
            "pour_pattern_code",
            "brewer_temperature_f_setting_raw",
            "pod_id",
            "preset_state_hex",
            "last_protocol_error",
            "model_raw_hex",
            "model",
            "area_ap_raw",
            "system_status_code",
            "user_count_raw",
            "voltage_raw",
            "pour_radius_baseline",
            "vibration_baseline",
        ]
    )
)
