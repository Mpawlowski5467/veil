# Configuration and local storage

[Back to the README](../README.md)

For names and other exact values, start with [private registration commands](entities.md). For custom patterns and advanced settings, edit the configuration below.

## Settings

Create a private configuration folder if needed:

```bash
mkdir -p ~/.veil
chmod 700 ~/.veil
```

Merge the settings you need into `~/.veil/config.json`:

```json
{
  "entities": {
    "PERSON": ["Jan Nowak"],
    "CLIENT": ["Example Corp"]
  },
  "patterns": {
    "ORDER": "#\\d{5}"
  },
  "identity": true,
  "retention_days": 30,
  "note": true,
  "allow_mcp_tools": []
}
```

Every setting is optional:

| Setting | Purpose |
| --- | --- |
| `entities` | Exact names, organizations, and other values to mask, grouped by type. |
| `patterns` | Additional regular expressions, grouped by type. |
| `identity` | Register your Git name and email. Defaults to `true`. |
| `retention_days` | Purge sessions unused for this many days when the gateway opens its storage. Defaults to `30`. |
| `note` | Tell the model to preserve placeholders. Defaults to `true`. |
| `secret_review` | Hold uncertain secret candidates for [local review](secret-review.md). Defaults to `false`; source checkout after 0.5.0 only. Restart the gateway after changing it. |
| `allow_mcp_tools` | MCP tools allowed to receive real values. Empty by default. |

Veil reads its configuration from `~/.veil`, not from a cloned project's files. Unknown settings and invalid values are rejected. The global `--data-dir` option selects a different configuration and storage folder.

## Session storage

Mappings live in `~/.veil/vault.db`. The gateway also keeps masked replies and hashes in `~/.veil/ledger.db` so conversation history can be replayed consistently.

The vault contains original values in plaintext. Veil creates private files with owner-only permissions; it does not encrypt the database. Claude Code's own local transcripts also contain restored values.

```bash
veil forget --session SESSION_ID
veil forget --all
```

These commands delete Veil's stored session mappings and ledger entries. They do not erase Claude Code's transcripts.
