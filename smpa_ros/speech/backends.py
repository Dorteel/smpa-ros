"""Preserve speech backend imports while inference is owned by smpa_llm."""
import sys
from smpa_llm.inference import speech as implementation

sys.modules[__name__] = implementation
