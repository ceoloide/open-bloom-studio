"""Independent open-source tools for xBloom Studio machines.

Read-only telemetry, explicitly opted-in controls and pluggable byte-link
connectors. Bluetooth support needs the optional ``ble`` extra.
"""

from .client import ProtocolCommandError, ReadOnlyClient, SessionError
from .connector import Connector, normalize_identity
from .control import (
    BusyError,
    EncodedRecipe,
    Interaction,
    Operation,
    UnsupportedStageError,
)
from .events import decode_event
from .intents import Studio, capabilities, plan
from .machine_controls import (
    EncodedTeaRecipe,
    PresetWriteError,
    SavedPreset,
    UntestedOperationError,
)
from .simulator import ScriptedConnector, SimulatedStudio
from .transport import BleakConnector, CommandHooks, discover

__all__ = [
    "BleakConnector",
    "BusyError",
    "CommandHooks",
    "Connector",
    "EncodedRecipe",
    "EncodedTeaRecipe",
    "Interaction",
    "Operation",
    "PresetWriteError",
    "ProtocolCommandError",
    "ReadOnlyClient",
    "SavedPreset",
    "ScriptedConnector",
    "SessionError",
    "SimulatedStudio",
    "Studio",
    "UnsupportedStageError",
    "UntestedOperationError",
    "capabilities",
    "decode_event",
    "discover",
    "normalize_identity",
    "plan",
]
