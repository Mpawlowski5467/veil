"""Show real local masking/restoration with a simulated reply and no network calls."""

from __future__ import annotations

import argparse
import json

from veil import Shield, __version__


def round_trip() -> dict:
    """Return the fictional example's actual results for text or visual output."""
    shield = Shield(redact_warnings=True)
    shield.add_entity("Jane Doe", "PERSON")
    original = (
        "Draft a reminder for Jane Doe.\n"
        "Email: jane.doe@example.com\n"
        "Phone: (202) 555-0147"
    )
    masked = shield.mask(original)
    if masked.warnings:
        raise RuntimeError("Review masking warnings before continuing.")

    # A fixed example reply: this demo does not contact an AI provider.
    reply = (
        "Hi [PERSON_1], your appointment is tomorrow.\nContact: [EMAIL_1] | [PHONE_1]"
    )
    restored = shield.restore(reply)
    if restored.warnings:
        raise RuntimeError("Review restoration warnings before continuing.")

    return {
        "version": __version__,
        "network_calls": 0,
        "reply_source": "simulated",
        "stages": [
            {"title": "Your original text", "text": original},
            {"title": "Masked text for the model", "text": masked.text},
            {"title": "Simulated model reply", "text": reply},
            {"title": "Reply restored locally", "text": restored.text},
        ],
    }


def main() -> None:
    """Print the example, or JSON used to render the shareable demo."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the demo data")
    args = parser.parse_args()
    result = round_trip()
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"Veil {result['version']} | local demo | no model call")
    print("Fictional data; Jane Doe is registered explicitly.\n")
    for number, stage in enumerate(result["stages"], start=1):
        print(f"{number}. {stage['title']}\n{stage['text']}\n")
    print("Masking and restoration ran locally. The model reply was simulated.")


if __name__ == "__main__":
    main()
