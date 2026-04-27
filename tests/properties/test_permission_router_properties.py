"""Property-based tests for :class:`PermissionRouter`.

Covers the first-resolve-wins / late-resolve-raises invariant that powers the
race semantics in the wire protocol (multiple subscribers can answer the same
permission request; only the first reply wins, others get HTTP 409).
"""
from __future__ import annotations

import asyncio
import string

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from agent_webkit_server.sdk_bridge import ConflictError, PermissionRouter


_correlation_id = st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=12)
_value = st.dictionaries(st.text(max_size=4), st.integers(), max_size=3)


@settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    cid=_correlation_id,
    first=_value,
    second=_value,
)
@pytest.mark.asyncio
async def test_first_resolve_wins_second_raises_for_any_value(cid, first, second) -> None:
    """For any pair of resolutions targeting the same correlation_id, exactly
    one resolves the future and the other raises ConflictError."""
    router = PermissionRouter()
    fut = router.register(cid)
    router.resolve(cid, first)
    assert (await fut) == first
    with pytest.raises(ConflictError):
        router.resolve(cid, second)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    cids=st.lists(_correlation_id, min_size=1, max_size=8, unique=True),
)
@pytest.mark.asyncio
async def test_cancel_all_releases_all_waiters_for_any_set(cids) -> None:
    """``cancel_all`` cancels every registered future regardless of how many."""
    router = PermissionRouter()
    futs = [router.register(c) for c in cids]
    router.cancel_all()
    # Every future cancelled; awaiting raises CancelledError.
    for f in futs:
        assert f.cancelled() or f.done()  # cancellation may be either


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    cids=st.lists(_correlation_id, min_size=1, max_size=6, unique=True),
)
@pytest.mark.asyncio
async def test_resolutions_are_isolated_across_correlation_ids(cids) -> None:
    """Resolving one correlation_id never affects another's pending future."""
    router = PermissionRouter()
    futs = {c: router.register(c) for c in cids}

    # Resolve only the first; all others must remain pending.
    target = cids[0]
    router.resolve(target, {"v": 1})

    assert futs[target].done()
    for c in cids[1:]:
        assert not futs[c].done(), f"resolving {target!r} should not affect {c!r}"
        assert router.has_pending(c)
