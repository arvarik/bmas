"""The Classic blackboard runtime package.

The package holds the two Classic runtimes and the policy modules the
legacy engine delegates to. The legacy pair binds the bare ``classic``
identifier and the ``traditional`` alias and runs the legacy adapter.
The native pair registers as a test-only pair with no alias, so only a
submission that names its exact contract version reaches it. The
registry registers both pairs in ``load_builtin_variants``.

The runtime classes load on first access. The policy modules are leaf
modules that the engine imports, and the adapter imports the engine, so
an eager import here would form a cycle.
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.variants.classic.adapter import ClassicHost, ClassicVariantRuntime
    from core.variants.classic.runtime import ClassicRuntime

__all__ = ["ClassicHost", "ClassicRuntime", "ClassicVariantRuntime"]

_RUNTIME_EXPORTS = {
    "ClassicHost": "core.variants.classic.adapter",
    "ClassicVariantRuntime": "core.variants.classic.adapter",
    "ClassicRuntime": "core.variants.classic.runtime",
}


def __getattr__(name: str) -> Any:
    module_name = _RUNTIME_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)
