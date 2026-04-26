"""Re-export :func:`fold_session_summary` from the SDK's internal module.

Centralized here so the import path is in one place — if the SDK ever
relocates the helper to a public module we only update this file.
"""
from claude_agent_sdk._internal.session_summary import fold_session_summary  # type: ignore

__all__ = ["fold_session_summary"]
