"""veil: reversible PII masking for LLM calls.

Mask personal information with placeholders before text reaches a model, then
restore the real values in the model's reply.

Example:
    >>> from veil import Shield
    >>> shield = Shield()
    >>> shield.add_entity("Jan Nowak", "PERSON")
    >>> shield.mask("Email Jan Nowak at jan.n@example.com").text
    'Email [PERSON_1] at [EMAIL_1]'
"""

from .detectors import Detector, ManualDetector, RegexDetector
from .shield import Shield
from .types import MaskedEntity, MaskResult, RestoreResult, ShieldWarning, Span
from .vault import MemoryVault, Vault

__version__ = "0.1.0"

__all__ = [
    "Detector",
    "ManualDetector",
    "MaskResult",
    "MaskedEntity",
    "MemoryVault",
    "RegexDetector",
    "RestoreResult",
    "Shield",
    "ShieldWarning",
    "Span",
    "Vault",
    "__version__",
]
