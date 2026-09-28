"""A local gateway that masks what a client sends to the model's API.

The default adapter serves Anthropic Messages for Claude Code. The experimental
OpenAI adapter serves Responses for manually configured API-key clients such as
Codex. Supported request text is masked and replies are restored locally.
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
from .openai_request import ResponsesRequestMasker
from .openai_response import ResponsesRestorer, ResponsesStreamRestorer
from .request import DEFAULT_NOTE, WITHHELD_LINE, RequestMasker, UnsupportedRequestError
from .response import ResponseRestorer, StreamError, restore_message
from .server import (
    OPENAI_SESSION_HEADER,
    SECRET_HEADER,
    SESSION_HEADER,
    Gateway,
    Session,
    Sessions,
)
from .store import SQLiteLedger, open_sessions, shield_factory

__all__ = [
    "DEFAULT_NOTE",
    "OPENAI_SESSION_HEADER",
    "SECRET_HEADER",
    "SESSION_HEADER",
    "WITHHELD_LINE",
    "Gateway",
    "Ledger",
    "MemoryLedger",
    "RequestMasker",
    "ResponseRestorer",
    "ResponsesRequestMasker",
    "ResponsesRestorer",
    "ResponsesStreamRestorer",
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
