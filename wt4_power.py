#!/usr/bin/env python3
"""Power-meter primitives for the future RTL-SDR integration.

This module deliberately has no RTL-SDR dependency yet. It defines the data
model and the small DSP helper we can test before wiring in hardware I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional


@dataclass(frozen=True)
class PowerMeterConfig:
    center_frequency_hz: int = 1_200_000_000
    sample_rate_hz: int = 1_024_000
    measurement_bandwidth_hz: int = 500_000
    update_rate_hz: float = 10.0
    device_index: int = 0
    gain_db: Optional[float] = None
    smoothing_samples: int = 3

    def validate(self) -> None:
        if self.center_frequency_hz <= 0:
            raise ValueError("center frequency must be positive")
        if self.sample_rate_hz <= 0:
            raise ValueError("sample rate must be positive")
        if not (0 < self.measurement_bandwidth_hz <= self.sample_rate_hz):
            raise ValueError("measurement bandwidth must be greater than zero and no wider than sample rate")
        if not (1.0 <= self.update_rate_hz <= 50.0):
            raise ValueError("update rate must be 1..50 Hz")
        if self.smoothing_samples < 1:
            raise ValueError("smoothing samples must be at least 1")

    @property
    def samples_per_update(self) -> int:
        return max(1, int(round(self.sample_rate_hz / self.update_rate_hz)))


@dataclass(frozen=True)
class PowerReading:
    timestamp: datetime
    power_dbfs: float
    sample_count: int


def power_dbfs(samples: Iterable[complex]) -> PowerReading:
    total = 0.0
    count = 0
    for sample in samples:
        total += sample.real * sample.real + sample.imag * sample.imag
        count += 1
    if count == 0:
        raise ValueError("at least one IQ sample is required")
    mean_power = total / count
    power = 10.0 * math.log10(max(mean_power, 1.0e-20))
    return PowerReading(datetime.now(timezone.utc), power, count)
