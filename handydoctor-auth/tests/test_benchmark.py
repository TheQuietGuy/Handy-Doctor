"""
tests/test_benchmark.py - the triage benchmark as a regression gate.

Fails the build if any emergency vignette is under-triaged, if anything is
under-triaged at all, or if exact accuracy drops below the recorded baseline.
"""

import pytest

from benchmark.run import run


@pytest.mark.asyncio
async def test_benchmark_safety_gate():
    res = await run(live=False)
    assert res["emergency_missed"] == [], f"emergencies under-triaged: {res['emergency_missed']}"
    unsafe = [r for r in res["rows"] if r["delta"] < 0]
    assert not unsafe, f"under-triaged: {unsafe}"
    assert res["exact"] >= 0.85, f"exact accuracy regressed to {res['exact']:.1%}"
