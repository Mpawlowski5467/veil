"""Local, authenticated CLI access to gateway activity and verification."""

from __future__ import annotations

import http.client
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .gateway import SECRET_HEADER, SettingsError, default_data_dir
from .gateway.activity import ACTIVITY_PATH, VERIFY_PATH
from .gateway.hooks import _gateway_answers, _secret_from_environment


@dataclass(frozen=True)
class Endpoint:
    """A loopback address and private credential; never display the credential."""

    url: str
    secret: str = field(repr=False)


def endpoint(
    *,
    config: Path | None = None,
    data_dir: Path | None = None,
    gateway_url: str | None = None,
) -> Endpoint:
    """Select an explicit gateway, inherited launcher, or saved Codex provider."""
    from .codex_setup import config_path, parse_toml, read_private_file

    if gateway_url is not None:
        url = gateway_url
        secret = read_private_file(
            (data_dir or default_data_dir()) / "gateway-secret"
        ).strip()
    elif config is None and (
        "VEIL_GATEWAY_URL" in os.environ or "VEIL_GATEWAY_SECRET" in os.environ
    ):
        url = os.environ.get("VEIL_GATEWAY_URL", "")
        secret = os.environ.get("VEIL_GATEWAY_SECRET", "")
    elif config is None and os.environ.get("ANTHROPIC_BASE_URL"):
        url = os.environ["ANTHROPIC_BASE_URL"]
        secret = _secret_from_environment() or ""
    else:
        document = parse_toml(read_private_file(config_path(config))).unwrap()
        providers = document.get("model_providers", {})
        provider = providers.get("veil", {}) if isinstance(providers, dict) else {}
        if document.get("model_provider") != "veil" or not isinstance(provider, dict):
            raise SettingsError(
                "saved Codex settings do not select Veil; configure it first"
            )
        url = provider.get("base_url", "")
        headers = provider.get("http_headers", {})
        secret = headers.get(SECRET_HEADER, "") if isinstance(headers, dict) else ""
    try:
        address = urlsplit(url) if isinstance(url, str) else None
        if (
            address is None
            or address.scheme != "http"
            or address.hostname != "127.0.0.1"
            or not address.port
            or address.username
            or address.password
            or address.path not in {"", "/", "/v1", "/v1/"}
            or address.query
            or address.fragment
            or not isinstance(secret, str)
            or not secret
            or "\n" in secret
            or "\r" in secret
        ):
            raise ValueError
    except ValueError:
        raise SettingsError(
            "a loopback Veil gateway and local secret are required"
        ) from None
    return Endpoint(f"http://127.0.0.1:{address.port}", secret)


def local_request(
    target: Endpoint, path: str, *, create: bool = False
) -> dict[str, Any]:
    """Prove gateway identity before sending its secret; never follow redirects."""
    if not _gateway_answers(target.url, target.secret):
        raise SettingsError("gateway is stopped or its identity could not be verified")
    connection = http.client.HTTPConnection(urlsplit(target.url).netloc, timeout=5)
    try:
        connection.request(
            "POST" if create else "GET",
            path,
            body=b"{}" if create else None,
            headers={SECRET_HEADER: target.secret},
        )
        response = connection.getresponse()
        if response.status == 404:
            raise SettingsError(
                "this gateway lacks activity/verification support; "
                "upgrade and restart it"
            )
        raw = response.read(512 * 1024 + 1)
        if response.status != 200 or len(raw) > 512 * 1024:
            raise SettingsError("the gateway could not return a local activity report")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (OSError, http.client.HTTPException, ValueError):
        raise SettingsError("the local gateway report could not be read") from None
    finally:
        connection.close()


def _token(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise SettingsError(
            "use the 32-character verification ID returned by veil verify"
        )
    return value


def run_verify(
    *,
    check: str | None = None,
    json_output: bool = False,
    config: Path | None = None,
    data_dir: Path | None = None,
    gateway_url: str | None = None,
) -> int:
    """Create a fictional prompt or check evidence from the client's actual call."""
    path = VERIFY_PATH + ("?id=" + _token(check) if check is not None else "")
    result = local_request(
        endpoint(config=config, data_dir=data_dir, gateway_url=gateway_url),
        path,
        create=check is None,
    )
    if json_output:
        print(json.dumps(result, indent=2))
    elif check is None:
        print(
            "Send this fictional test prompt in the conversation you want to verify "
            "within ten minutes. It makes a normal model call using that client's "
            "account; this command itself only contacts the local gateway.\n"
        )
        print(result["prompt"])
        print(
            f"\nThen run verify --check {result['verification_id']} "
            "with the same gateway options."
        )
    else:
        _print_probe(result)
    return 0 if check is None or result.get("state") == "verified" else 1


def _print_probe(result: dict[str, Any]) -> None:
    print(f"Verification: {result.get('state', 'unknown')}")
    if result.get("session_ref"):
        print(f"Observed session reference: {result['session_ref']}")
    for key in ("masked", "forwarded", "restored", "completed"):
        if key in result:
            print(f"  {key}: {result[key]}")
    print(
        "Evidence covers this test request only. Unknown/expired evidence needs a "
        "new probe; incomplete evidence needs troubleshooting and a new probe. "
        "It does not guarantee future routing or coverage of other traffic."
    )


def run_activity(
    *,
    verification: str | None = None,
    json_output: bool = False,
    config: Path | None = None,
    data_dir: Path | None = None,
    gateway_url: str | None = None,
) -> int:
    """Inspect volatile gateway evidence, optionally scoped to a specific probe."""
    if verification is not None:
        _token(verification)
    target = endpoint(config=config, data_dir=data_dir, gateway_url=gateway_url)
    result = local_request(target, ACTIVITY_PATH)
    result["gateway_state"] = "ready"
    result["verification"] = (
        local_request(target, VERIFY_PATH + "?id=" + verification)
        if verification is not None
        else None
    )
    if json_output:
        print(json.dumps(result, indent=2))
    else:
        print("Veil gateway: ready")
        for item in result["sessions"]:
            state = (
                "recently verified"
                if item["recently_verified"]
                else "observed, unverified"
            )
            last_request = datetime.fromtimestamp(
                item["last_request_at"], timezone.utc
            ).isoformat()
            print(
                f"  {item['session_ref']}: {state}; forwarded={item['forwarded']}, "
                f"completed={item['completed']}, failed={item['failed']}; "
                f"last request={last_request}"
            )
            print(
                "    Placeholder occurrences: "
                + json.dumps(item["placeholder_occurrences"], sort_keys=True)
            )
        if result["verification"] is not None:
            _print_probe(result["verification"])
        else:
            print(
                "No verification ID selected: do not assume any listed "
                "session is this task."
            )
        print(result["scope"])
    selected = result["verification"]
    return (
        0
        if verification is None or (selected and selected.get("state") == "verified")
        else 1
    )
