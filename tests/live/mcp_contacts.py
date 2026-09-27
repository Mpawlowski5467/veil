"""A minimal stdio MCP server for live tests, with one tool that returns PII.

Speaks newline-delimited JSON-RPC 2.0 on stdin and stdout, which is all a
local MCP server needs. The tool ``find_contact`` returns a fictional contact
card, so tests can check what reaches the model from an MCP tool.
"""

from __future__ import annotations

import json
import sys
from typing import Any

CONTACT = "Name: Jan Nowak\nEmail: jane.doe@example.com\nPhone: 555-0100\n"

TOOL = {
    "name": "find_contact",
    "description": "Look up a contact card by name.",
    "inputSchema": {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Who to look up"}},
    },
}


def reply(request: dict[str, Any]) -> dict[str, Any] | None:
    """Return the JSON-RPC response for one message, or None for notifications."""
    if "id" not in request:
        return None
    method = request.get("method")
    result: dict[str, Any]
    if method == "initialize":
        params = request.get("params") or {}
        result = {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "contacts", "version": "1.0"},
        }
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": CONTACT}], "isError": False}
    elif method == "ping":
        result = {}
    else:
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {"code": -32601, "message": f"unknown method {method}"},
        }
    return {"jsonrpc": "2.0", "id": request["id"], "result": result}


def main() -> None:
    """Serve requests until stdin closes."""
    for line in sys.stdin:
        if not line.strip():
            continue
        response = reply(json.loads(line))
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
