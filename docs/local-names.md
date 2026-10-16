# Optional local name recognition (unreleased)

Local name recognition is in the 1.0 plan and source checkout. It is disabled by
default and requires an extra plus a trusted local spaCy NER pipeline. The base
library remains dependency-free and does not import spaCy when recognition is off.

```bash
python -m pip install '.[names]'
veil names enable --model /absolute/path/to/local/pipeline
veil names status --json
veil restart
veil names disable
```

Choose a pipeline for the language you actually use, obtain it separately, and
select its unpacked data directory containing `config.cfg`, `meta.json`, and
component data. Veil accepts local directories, not package names or URLs, and
does not run a model download command. See spaCy's official
[pipeline installation guide](https://spacy.io/usage/models) and
[local-path loading API](https://spacy.io/api/top-level#spacy.load).
Only use trusted models: pipeline configuration and registered components can
execute code. No trained model or language-quality claim is bundled with Veil.

The desktop's Choose local model control performs the same validation and edit.
Use the same global `--data-dir PATH` as the intended gateway. Settings changes
apply to new gateways and local mask commands; restart existing gateways or
relaunch clients. Disabling detection does not delete existing mappings.

## Detected types and boundaries

The detector maps `PERSON`/`PER` to `PERSON`, `ORG` to `ORGANIZATION`, and
`GPE`/`LOC` to `LOCATION`. Other NER labels are ignored. Recognized spans retain
exact Unicode character offsets and share existing reversible placeholders.
Registered values retain priority over equal-length model detections. Default
regex secret/PII masking still runs alongside recognition.

Gateway conversations share one loaded model, with serialized inference and
separate vaults. Veil neither saves raw NER inputs in a recognition log nor sends
them to a remote classifier. Ordinary gateway/session storage still contains
original values under the existing plaintext-vault policy.

Model selection fails before changing config if the runtime/model is missing or
the pipeline has no NER component. Configured model failures prevent gateway
startup or request processing; they do not silently turn recognition off.
Oversized input is refused at the model's `max_length`, rather than truncated
or split across potential name boundaries. Shorten the input or adjust your
trusted model; there is no automatic unmasked fallback.

Recognition can miss names, confuse public and private entities, over-mask code,
and vary by language, casing, context, or model version. Keep exact registrations
for important values, preview representative text locally, and retain explicit
review where needed. Current fictional regressions and a tiny locally trained
pipeline check model loading, offset handling, failure behavior, and restoration;
they do not measure real-world recall or false positives. Model selection,
independent quality measurements, memory/latency budgets, multilingual/code
coverage, and redistribution/licensing decisions remain 1.0 validation work.

## Python integration

```python
from pathlib import Path
from veil import LocalNameDetector, RegexDetector, Shield

shield = Shield(
    detectors=[
        RegexDetector(),
        LocalNameDetector(Path("/absolute/path/to/local/pipeline")),
    ]
)
```

## Configuration and rollback

The CLI safely merges `local_names_model` into private `config.json`, preserving
other settings and refusing concurrent or unsafe edits. An absolute path enables
recognition; `null` disables it. Model paths are omitted from `names status`.
Older releases reject this new key even when null. Before rolling back, stop
writers and restore your pre-upgrade configuration, or remove only this key from
your private configuration after making a backup. Existing mappings still need
the normal upgrade/rollback validation.
