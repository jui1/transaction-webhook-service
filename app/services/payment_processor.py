"""Stand-in for a slow payment API. The worker calls this. The webhook does not."""

import time


def simulate_external_payment_call(delay_seconds: float) -> None:
    """Sleep for the configured delay. Only the worker should call this."""
    if delay_seconds < 0:
        raise ValueError("delay_seconds must be non-negative")
    if delay_seconds > 0:
        time.sleep(delay_seconds)
