"""Shared test doubles."""

from datetime import timedelta

from symx.common import Timeout


class FakeTimeout(Timeout):
    """A Timeout that uses a manually advanceable clock instead of real time."""

    def __init__(self, limit: timedelta) -> None:
        super().__init__(limit)
        self._elapsed = 0.0

    def exceeded(self) -> bool:
        return self._elapsed > self._limit_seconds

    @property
    def elapsed_seconds(self) -> int:
        return int(self._elapsed)

    def advance(self, seconds: float) -> None:
        self._elapsed += seconds
