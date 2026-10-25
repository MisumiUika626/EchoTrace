"""Deterministic offline token counting with explicit approximation metadata."""
import math
from dataclasses import dataclass

from echotrace.util import dumps


@dataclass(frozen=True)
class TokenCount:
    tokens: int
    name: str
    approximate: bool


def count_tokens(value, model=None):
    text = value if isinstance(value, str) else dumps(value)
    weighted_characters = sum(1 if ord(character) > 127 else .3 for character in text)
    return TokenCount(max(1, math.ceil(weighted_characters * 1.25)),
                      "conservative-character-estimator-v1", True)
