"""The Classic blackboard runtime package.

The package registers both Classic pairs. The legacy pair binds the
bare ``classic`` identifier and the ``traditional`` alias and runs the
legacy adapter. The native pair registers as a test-only pair with no
alias, so only a submission that names its exact contract version
reaches it.
"""
from __future__ import annotations

from core.variants import (
    CLASSIC_VARIANT,
    LEGACY_CLASSIC_VARIANT,
    TEST_ONLY_AVAILABILITY,
    register_variant,
)
from core.variants.classic.adapter import ClassicHost, ClassicVariantRuntime
from core.variants.classic.runtime import ClassicRuntime

__all__ = ["ClassicHost", "ClassicRuntime", "ClassicVariantRuntime"]

register_variant(
    CLASSIC_VARIANT,
    ClassicVariantRuntime,
    aliases=(LEGACY_CLASSIC_VARIANT,),
)
register_variant(
    CLASSIC_VARIANT,
    ClassicRuntime,
    availability=TEST_ONLY_AVAILABILITY,
    bind_aliases=False,
)
