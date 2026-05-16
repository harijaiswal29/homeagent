"""Pluggable check registry.

Each check is a callable registered with `@register_check(name, weight)` that takes
`(listing, project, context)` and returns a `CheckResult`. New checks can be dropped into
`homeagent/verification/` as new modules — `discover_checks()` imports them so the decorator
runs and the registry populates.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from homeagent.models import Listing, Project

log = logging.getLogger(__name__)

Verdict = Literal["pass", "warn", "fail", "unknown"]


@dataclass
class CheckResult:
    verdict: Verdict
    score: float  # 0.0 (fail) .. 1.0 (pass)
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class _RegisteredCheck:
    name: str
    weight: float
    fn: Callable[..., CheckResult]


_REGISTRY: dict[str, _RegisteredCheck] = {}


def register_check(
    name: str, weight: float = 0.1
) -> Callable[[Callable[..., CheckResult]], Callable[..., CheckResult]]:
    """Decorator: register a check function so the agent can run it."""

    def deco(fn: Callable[..., CheckResult]) -> Callable[..., CheckResult]:
        if name in _REGISTRY:
            log.warning("re-registering check %r — replacing previous", name)
        _REGISTRY[name] = _RegisteredCheck(name=name, weight=weight, fn=fn)
        return fn

    return deco


def list_checks() -> list[_RegisteredCheck]:
    return list(_REGISTRY.values())


def run_check(
    name: str,
    listing: Listing,
    project: Project | None = None,
    context: dict[str, Any] | None = None,
) -> CheckResult:
    check = _REGISTRY[name]
    return check.fn(listing=listing, project=project, context=context or {})


def discover_checks() -> None:
    """Import all sibling modules in `homeagent.verification` so their decorators run."""
    import homeagent.verification as pkg

    for mod_info in pkgutil.iter_modules(pkg.__path__):
        if mod_info.name in {"registry", "__init__"}:
            continue
        full = f"{pkg.__name__}.{mod_info.name}"
        try:
            importlib.import_module(full)
        except Exception as e:
            log.warning("failed to import check module %s: %s", full, e)


def clear_registry_for_tests() -> None:
    """Reset the registry — only for tests."""
    _REGISTRY.clear()
