"""Preserve the legacy module identity for imports and dependency injection."""
import sys
from smpa_llm.inference.backends import api as implementation

sys.modules[__name__] = implementation
