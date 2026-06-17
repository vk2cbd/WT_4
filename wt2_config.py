#!/usr/bin/env python3
"""Configuration helpers for WT_2."""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import Union

from wt2_driver import AntennaConfig, Calibration, SafetyLimits


def load_configs(path: Union[str, Path]) -> dict[str, AntennaConfig]:
    path = Path(path)
    parser = configparser.ConfigParser()
    if not path.exists():
        return {}
    parser.read(path)

    configs: dict[str, AntennaConfig] = {}
    for section in parser.sections():
        if not section.startswith("antenna:"):
            continue
        name = section.split(":", 1)[1].strip()
        port = parser.get(section, "port", fallback="").strip()
        if not name or not port:
            continue
        configs[name] = AntennaConfig(
            name=name,
            port=port,
            baud=parser.getint(section, "baud", fallback=9600),
            open_delay=parser.getfloat(section, "open_delay", fallback=5.0),
            gui_speed=parser.getint(section, "gui_speed", fallback=40),
            calibration=Calibration(
                az_offset=parser.getfloat(section, "az_offset", fallback=0.0),
                el_offset=parser.getfloat(section, "el_offset", fallback=0.0),
            ),
            limits=SafetyLimits(
                az_min=parser.getfloat(section, "az_min", fallback=270.0),
                az_max=parser.getfloat(section, "az_max", fallback=265.0),
                el_min=parser.getfloat(section, "el_min", fallback=0.0),
                el_max=parser.getfloat(section, "el_max", fallback=87.0),
                az_margin=parser.getfloat(section, "az_margin", fallback=0.5),
                el_margin=parser.getfloat(section, "el_margin", fallback=0.5),
                max_jog_seconds=parser.getfloat(section, "max_jog_seconds", fallback=60.0),
                poll_interval=parser.getfloat(section, "poll_interval", fallback=0.2),
            ),
        )
    return configs


def save_configs(path: Union[str, Path], configs: dict[str, AntennaConfig]) -> None:
    path = Path(path)
    parser = configparser.ConfigParser()
    for name, config in configs.items():
        section = f"antenna:{name}"
        parser[section] = {
            "port": config.port,
            "baud": str(config.baud),
            "open_delay": f"{config.open_delay:g}",
            "gui_speed": str(config.gui_speed),
            "az_offset": f"{config.calibration.az_offset:.6f}",
            "el_offset": f"{config.calibration.el_offset:.6f}",
            "az_min": f"{config.limits.az_min:.3f}",
            "az_max": f"{config.limits.az_max:.3f}",
            "el_min": f"{config.limits.el_min:.3f}",
            "el_max": f"{config.limits.el_max:.3f}",
            "az_margin": f"{config.limits.az_margin:.3f}",
            "el_margin": f"{config.limits.el_margin:.3f}",
            "max_jog_seconds": f"{config.limits.max_jog_seconds:.3f}",
            "poll_interval": f"{config.limits.poll_interval:.3f}",
        }
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)
