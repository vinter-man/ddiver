"""Fixtures shared by the whole suite.

Only the ones that were byte-identical in six files live here; anything a single module seeds
for itself stays in that module, where the reader can see what the test is standing on.
"""

from __future__ import annotations

import pytest

from ddiver_core_testing import FakeClock


@pytest.fixture
def clock() -> FakeClock:
    """A clock the test drives by hand, so nothing in the suite waits on wall time."""
    return FakeClock()
