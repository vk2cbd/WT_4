#!/usr/bin/env python3
"""Command-line RTL-SDR power meter for WT4."""

from __future__ import annotations

import argparse
import sys
import time

from wt4_power import PowerMeterConfig, RtlPowerMeter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream RTL-SDR relative power readings.")
    parser.add_argument("--freq", type=int, default=1_200_000_000, help="Center frequency in Hz.")
    parser.add_argument("--rate", type=int, default=1_024_000, help="RTL sample rate in samples/second.")
    parser.add_argument("--bandwidth", type=int, default=500_000, help="Measurement bandwidth hint in Hz.")
    parser.add_argument("--update-rate", type=float, default=10.0, help="Power update rate in Hz.")
    parser.add_argument("--device", type=int, default=0, help="RTL-SDR device index.")
    parser.add_argument("--gain", type=float, default=None, help="Manual tuner gain in dB. Omit for auto gain.")
    parser.add_argument("--count", type=int, default=0, help="Number of readings to print. 0 means continuous.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = PowerMeterConfig(
        center_frequency_hz=args.freq,
        sample_rate_hz=args.rate,
        measurement_bandwidth_hz=args.bandwidth,
        update_rate_hz=args.update_rate,
        device_index=args.device,
        gain_db=args.gain,
    )
    config.validate()
    print(
        "RTL power meter: "
        f"freq={config.center_frequency_hz} Hz "
        f"rate={config.sample_rate_hz} sps "
        f"updates={config.update_rate_hz:0.1f} Hz "
        f"samples/update={config.samples_per_update} "
        f"gain={'auto' if config.gain_db is None else f'{config.gain_db:0.1f} dB'}",
        flush=True,
    )

    readings = 0
    try:
        with RtlPowerMeter(config) as meter:
            while args.count <= 0 or readings < args.count:
                started = time.monotonic()
                reading = meter.read_power()
                readings += 1
                print(f"{reading.timestamp.astimezone():%Y-%m-%d %H:%M:%S.%f %Z} {reading.power_dbfs:8.2f} dBFS")
                target_period = 1.0 / config.update_rate_hz
                elapsed = time.monotonic() - started
                if elapsed < target_period:
                    time.sleep(target_period - elapsed)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
