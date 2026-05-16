"""Verification checks. New checks register via `@register_check`."""

from homeagent.verification.registry import (
    CheckResult,
    discover_checks,
    list_checks,
    register_check,
    run_check,
)

__all__ = ["CheckResult", "discover_checks", "list_checks", "register_check", "run_check"]
