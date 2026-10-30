from typing import Optional


def avalanche_key(apr: Optional[float], balance: Optional[float]) -> tuple:
    """Highest APR first, unknown APR last, larger balance first on an APR tie."""
    return apr is None, -(apr or 0), -(balance or 0)
