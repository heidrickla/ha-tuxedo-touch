"""Load one integration module without importing Home Assistant.

The package's __init__ imports Home Assistant, so importing
`custom_components.tuxedo_touch.push` the normal way would drag the whole
framework in. api.py, const.py and push.py depend on nothing but aiohttp and
cryptography, and the tests for them are worth being able to run anywhere.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMP = os.path.join(ROOT, "custom_components", "tuxedo_touch")

_pkg = types.ModuleType("tuxedo_touch")
_pkg.__path__ = [COMP]  # type: ignore[attr-defined]
sys.modules.setdefault("tuxedo_touch", _pkg)


def load(name: str) -> Any:
    """Import `<name>.py` from the integration as `tuxedo_touch.<name>`."""
    if (already := sys.modules.get(f"tuxedo_touch.{name}")) is not None:
        return already
    spec = importlib.util.spec_from_file_location(
        f"tuxedo_touch.{name}", os.path.join(COMP, f"{name}.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"tuxedo_touch.{name}"] = module
    spec.loader.exec_module(module)
    return module
