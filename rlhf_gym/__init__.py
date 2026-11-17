"""Compatibility imports for the shared package's independently runnable gym."""
import sys
from importlib import import_module

for name in ('vault', 'inference', 'training'):
    module = import_module('smpa_llm.training.rlhf.' + name)
    globals()[name] = module
    sys.modules[__name__ + '.' + name] = module
