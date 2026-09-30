"""Strict typing surface for the upstream, unmodified basic_metrics.py."""

import numpy as np
from numpy.typing import NDArray

def generate_curve(
    label: NDArray[np.int_],
    score: NDArray[np.float64],
    slidingWindow: int,
    version: str = "opt",
    thre: int = 250,
) -> tuple[
    NDArray[np.float64],
    NDArray[np.int_],
    NDArray[np.float64],
    NDArray[np.float64],
    NDArray[np.float64],
    NDArray[np.int_],
    float,
    float,
]: ...
