"""One monotonic deadline shared by both participants in an episode."""

import time


class EpisodeTimeoutError(TimeoutError):
    """The episode deadline expired while a participant was requesting a response."""

    def __init__(self, role):
        self.role = role
        super().__init__(f"Episode wall-clock limit reached during {role} response")


def remaining_seconds(deadline, role):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise EpisodeTimeoutError(role)
    return remaining
