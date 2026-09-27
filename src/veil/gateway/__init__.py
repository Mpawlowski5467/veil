"""A local gateway that masks what a client sends to the model's API.

A client such as Claude Code, pointed at the gateway with
``ANTHROPIC_BASE_URL``, sends its Messages API requests here. Each request is
masked (every text the model would read), forwarded, and the streamed reply
restored before the client sees it.
"""

from .config import (
    Settings,
    SettingsError,
    default_data_dir,
    git_identity,
    load_settings,
    prepare_data_dir,
)
from .ledger import Ledger, MemoryLedger
from .request import DEFAULT_NOTE, WITHHELD_LINE, RequestMasker, UnsupportedRequestError
from .response import ResponseRestorer, StreamError, restore_message
from .server import SECRET_HEADER, SESSION_HEADER, Gateway, Session, Sessions
from .store import SQLiteLedger, open_sessions, shield_factory

__all__ = [
    "DEFAULT_NOTE",
    "SECRET_HEADER",
    "SESSION_HEADER",
    "WITHHELD_LINE",
    "Gateway",
    "Ledger",
    "MemoryLedger",
    "RequestMasker",
    "ResponseRestorer",
    "SQLiteLedger",
    "Session",
    "Sessions",
    "Settings",
    "SettingsError",
    "StreamError",
    "UnsupportedRequestError",
    "default_data_dir",
    "git_identity",
    "load_settings",
    "open_sessions",
    "prepare_data_dir",
    "restore_message",
    "shield_factory",
]
