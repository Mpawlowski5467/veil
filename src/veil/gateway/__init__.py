"""A local gateway that masks what a client sends to the model's API.

A client such as Claude Code, pointed at the gateway with
``ANTHROPIC_BASE_URL``, sends its Messages API requests here. Each request is
masked (every text the model would read), forwarded, and the streamed reply
restored before the client sees it.
"""

from .ledger import Ledger, MemoryLedger
from .request import DEFAULT_NOTE, WITHHELD_LINE, RequestMasker, UnsupportedRequestError

__all__ = [
    "DEFAULT_NOTE",
    "WITHHELD_LINE",
    "Ledger",
    "MemoryLedger",
    "RequestMasker",
    "UnsupportedRequestError",
]
