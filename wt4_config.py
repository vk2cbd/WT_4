#!/usr/bin/env python3
"""Configuration helpers for WT4."""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from wt4_driver import AntennaConfig, Calibration, SafetyLimits


@dataclass
class SourceConfig:
    name: str
    ra_hours: float = 0.0
    dec_degrees: float = 0.0
    flux_4800_mhz: float = 0.0


@dataclass
class SiteConfig:
    latitude: float = -32.724000
    longitude: float = 152.130167
    selected_source: str = ""
    track_interval_seconds: float = 2.0
    az_track_tolerance_degrees: float = 0.10
    el_track_tolerance_degrees: float = 0.10
    az_stop_tolerance_degrees: float = 0.10
    el_stop_tolerance_degrees: float = 0.10
    az_slow_speed: int = 20
    el_slow_speed: int = 20
    az_slow_threshold_degrees: float = 3.0
    el_slow_threshold_degrees: float = 3.0


def load_site_config(path: Union[str, Path]) -> SiteConfig:
    path = Path(path)
    parser = configparser.ConfigParser()
    if path.exists():
        parser.read(path)
    old_tolerance = parser.getfloat("site", "track_tolerance_degrees", fallback=0.10)
    old_slow_speed = parser.getint("site", "slow_speed", fallback=20)
    old_slow_threshold = parser.getfloat("site", "slow_threshold_degrees", fallback=3.0)
    az_start_tolerance = parser.getfloat("site", "az_track_tolerance_degrees", fallback=old_tolerance)
    el_start_tolerance = parser.getfloat("site", "el_track_tolerance_degrees", fallback=old_tolerance)
    return SiteConfig(
        latitude=parser.getfloat("site", "latitude", fallback=-32.724000),
        longitude=parser.getfloat("site", "longitude", fallback=152.130167),
        selected_source=parser.get("site", "selected_source", fallback="").strip(),
        track_interval_seconds=parser.getfloat("site", "track_interval_seconds", fallback=2.0),
        az_track_tolerance_degrees=az_start_tolerance,
        el_track_tolerance_degrees=el_start_tolerance,
        az_stop_tolerance_degrees=parser.getfloat(
            "site", "az_stop_tolerance_degrees", fallback=abs(az_start_tolerance)
        ),
        el_stop_tolerance_degrees=parser.getfloat(
            "site", "el_stop_tolerance_degrees", fallback=abs(el_start_tolerance)
        ),
        az_slow_speed=parser.getint("site", "az_slow_speed", fallback=old_slow_speed),
        el_slow_speed=parser.getint("site", "el_slow_speed", fallback=old_slow_speed),
        az_slow_threshold_degrees=parser.getfloat("site", "az_slow_threshold_degrees", fallback=old_slow_threshold),
        el_slow_threshold_degrees=parser.getfloat("site", "el_slow_threshold_degrees", fallback=old_slow_threshold),
    )


def save_site_config(path: Union[str, Path], site: SiteConfig) -> None:
    path = Path(path)
    parser = configparser.ConfigParser()
    if path.exists():
        parser.read(path)
    parser["site"] = _site_section(site)
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def load_sources(path: Union[str, Path]) -> dict[str, SourceConfig]:
    path = Path(path)
    parser = configparser.ConfigParser()
    if not path.exists():
        return _default_sources()
    parser.read(path)

    sources: dict[str, SourceConfig] = {}
    for section in parser.sections():
        if not section.startswith("source:"):
            continue
        name = section.split(":", 1)[1].strip()
        if not name:
            continue
        sources[name] = SourceConfig(
            name=name,
            ra_hours=parser.getfloat(section, "ra_hours", fallback=0.0),
            dec_degrees=parser.getfloat(section, "dec_degrees", fallback=0.0),
            flux_4800_mhz=parser.getfloat(section, "flux_4800_mhz", fallback=0.0),
        )
    return sources or _default_sources()


def save_sources(path: Union[str, Path], sources: dict[str, SourceConfig], selected_source: str) -> None:
    path = Path(path)
    parser = configparser.ConfigParser()
    if path.exists():
        parser.read(path)
    if not parser.has_section("site"):
        parser["site"] = _site_section(SiteConfig(selected_source=selected_source))
    else:
        parser["site"]["selected_source"] = selected_source
    for section in list(parser.sections()):
        if section.startswith("source:"):
            parser.remove_section(section)
    for name, source in sources.items():
        parser[f"source:{name}"] = {
            "ra_hours": f"{source.ra_hours:.6f}",
            "dec_degrees": f"{source.dec_degrees:.6f}",
            "flux_4800_mhz": f"{source.flux_4800_mhz:.3f}",
        }
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


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
            az_track_speed=parser.getint(section, "az_track_speed", fallback=parser.getint(section, "gui_speed", fallback=40)),
            el_track_speed=parser.getint(section, "el_track_speed", fallback=parser.getint(section, "gui_speed", fallback=40)),
            park_az=parser.getfloat(section, "park_az", fallback=355.0),
            park_el=parser.getfloat(section, "park_el", fallback=80.0),
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
    if path.exists():
        parser.read(path)
    if not parser.has_section("site"):
        parser["site"] = _site_section(SiteConfig())
    for section in list(parser.sections()):
        if section.startswith("antenna:"):
            parser.remove_section(section)
    for name, config in configs.items():
        section = f"antenna:{name}"
        parser[section] = {
            "port": config.port,
            "baud": str(config.baud),
            "open_delay": f"{config.open_delay:g}",
            "gui_speed": str(config.gui_speed),
            "az_track_speed": str(config.az_track_speed),
            "el_track_speed": str(config.el_track_speed),
            "park_az": f"{config.park_az:.3f}",
            "park_el": f"{config.park_el:.3f}",
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


def _site_section(site: SiteConfig) -> dict[str, str]:
    return {
        "latitude": f"{site.latitude:.6f}",
        "longitude": f"{site.longitude:.6f}",
        "selected_source": site.selected_source,
        "track_interval_seconds": f"{site.track_interval_seconds:.1f}",
        "az_track_tolerance_degrees": f"{site.az_track_tolerance_degrees:.2f}",
        "el_track_tolerance_degrees": f"{site.el_track_tolerance_degrees:.2f}",
        "az_stop_tolerance_degrees": f"{site.az_stop_tolerance_degrees:.2f}",
        "el_stop_tolerance_degrees": f"{site.el_stop_tolerance_degrees:.2f}",
        "az_slow_speed": str(max(0, min(100, int(site.az_slow_speed)))),
        "el_slow_speed": str(max(0, min(100, int(site.el_slow_speed)))),
        "az_slow_threshold_degrees": f"{site.az_slow_threshold_degrees:.1f}",
        "el_slow_threshold_degrees": f"{site.el_slow_threshold_degrees:.1f}",
    }


def _default_sources() -> dict[str, SourceConfig]:
    return {
        "Virgo A": SourceConfig("Virgo A", ra_hours=12.5137, dec_degrees=12.3911, flux_4800_mhz=70.0),
        "Centaurus A": SourceConfig("Centaurus A", ra_hours=13.4241, dec_degrees=-43.0191, flux_4800_mhz=650.0),
        "Orion A": SourceConfig("Orion A", ra_hours=5.5881, dec_degrees=-5.3911, flux_4800_mhz=400.0),
    }
