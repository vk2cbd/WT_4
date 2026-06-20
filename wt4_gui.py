#!/usr/bin/env python3
"""WT4 two-antenna safety/calibration GUI."""

from __future__ import annotations

import argparse
import csv
import math
import queue
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Optional

from wt4_astro import TargetPosition, local_sidereal_time, moon_equatorial, moon_position, source_position
from wt4_config import (
    PowerConfig,
    ScanConfig,
    SiteConfig,
    SourceConfig,
    load_configs,
    load_power_config,
    load_scan_config,
    load_site_config,
    load_sources,
    save_configs,
    save_power_config,
    save_scan_config,
    save_site_config,
    save_sources,
)
from wt4_driver import AntennaConfig, Axis, Direction, EncoderInfo, Position, SafeAntenna, shortest_angle_delta
from wt4_power import PowerMeterConfig, PowerReading, RtlPowerMeter
from wt4_solar import sun_equatorial, sun_position


APP_VERSION = "v4.0"


def axis_label(axis: Axis) -> str:
    return "AZ" if axis == Axis.AZIMUTH else "EL"


class LimitsDialog(tk.Toplevel):
    def __init__(self, app: "WT4App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Antenna Limits")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.entries: dict[str, dict[str, tk.StringVar]] = {}
        self.park_entries: dict[str, dict[str, tk.StringVar]] = {}

        tabs = ttk.Notebook(self)
        tabs.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")

        for name, config in app.configs.items():
            frame = ttk.Frame(tabs, padding=10)
            tabs.add(frame, text=name)
            self.entries[name] = self._build_limit_fields(frame, config)

        park_frame = ttk.Frame(tabs, padding=10)
        tabs.add(park_frame, text="Park")
        self._build_park_fields(park_frame)

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(buttons, text="Save", command=self.save).grid(row=0, column=2)

    def _build_limit_fields(self, frame: ttk.Frame, config: AntennaConfig) -> dict[str, tk.StringVar]:
        values = {
            "az_min": tk.StringVar(value=f"{config.limits.az_min:0.0f}"),
            "az_max": tk.StringVar(value=f"{config.limits.az_max:0.0f}"),
            "el_min": tk.StringVar(value=f"{config.limits.el_min:0.0f}"),
            "el_max": tk.StringVar(value=f"{config.limits.el_max:0.0f}"),
            "az_margin": tk.StringVar(value=f"{config.limits.az_margin:0.1f}"),
            "el_margin": tk.StringVar(value=f"{config.limits.el_margin:0.1f}"),
            "max_jog_seconds": tk.StringVar(value=f"{config.limits.max_jog_seconds:0.0f}"),
            "poll_interval": tk.StringVar(value=f"{config.limits.poll_interval:0.1f}"),
        }
        labels = [
            ("AZ min", "az_min"),
            ("AZ max", "az_max"),
            ("EL min", "el_min"),
            ("EL max", "el_max"),
            ("AZ margin", "az_margin"),
            ("EL margin", "el_margin"),
            ("Max jog sec", "max_jog_seconds"),
            ("Poll sec", "poll_interval"),
        ]
        for row, (label, key) in enumerate(labels):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(frame, textvariable=values[key], width=9).grid(row=row, column=1, sticky="w", pady=2)
        return values

    def _build_park_fields(self, frame: ttk.Frame) -> None:
        ttk.Label(frame, text="Antenna").grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(frame, text="Park AZ").grid(row=0, column=1, sticky="w", pady=(0, 4))
        ttk.Label(frame, text="Park EL").grid(row=0, column=2, sticky="w", pady=(0, 4))
        for row, (name, config) in enumerate(self.app.configs.items(), start=1):
            values = {
                "park_az": tk.StringVar(value=f"{config.park_az:0.0f}"),
                "park_el": tk.StringVar(value=f"{config.park_el:0.0f}"),
            }
            self.park_entries[name] = values
            ttk.Label(frame, text=name).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(frame, textvariable=values["park_az"], width=9).grid(row=row, column=1, sticky="w", padx=(8, 0), pady=2)
            ttk.Entry(frame, textvariable=values["park_el"], width=9).grid(row=row, column=2, sticky="w", padx=(8, 0), pady=2)

    def save(self) -> None:
        parsed: dict[str, dict[str, float]] = {}
        parsed_park: dict[str, dict[str, float]] = {}
        try:
            for name, values in self.entries.items():
                parsed[name] = {key: float(value.get()) for key, value in values.items()}
                self._validate_limits(name, parsed[name])
            for name, values in self.park_entries.items():
                parsed_park[name] = {key: float(value.get()) for key, value in values.items()}
                self._validate_park(name, parsed_park[name], parsed[name])
        except ValueError:
            messagebox.showerror("Invalid Limits", "All limit values must be numeric.", parent=self)
            return
        except RuntimeError as exc:
            messagebox.showerror("Invalid Limits", str(exc), parent=self)
            return

        for name, values in parsed.items():
            limits = self.app.configs[name].limits
            limits.az_min = values["az_min"]
            limits.az_max = values["az_max"]
            limits.el_min = values["el_min"]
            limits.el_max = values["el_max"]
            limits.az_margin = values["az_margin"]
            limits.el_margin = values["el_margin"]
            limits.max_jog_seconds = values["max_jog_seconds"]
            limits.poll_interval = values["poll_interval"]
            self.app.configs[name].park_az = parsed_park[name]["park_az"]
            self.app.configs[name].park_el = parsed_park[name]["park_el"]
            if name in self.app.panels:
                self.app.panels[name].sync_config_settings()

        self._format_fields(parsed)
        self._format_park_fields(parsed_park)
        self.app.save_config("Limits saved.")
        self.destroy()

    def _format_fields(self, parsed: dict[str, dict[str, float]]) -> None:
        formats = {
            "az_min": "{:0.0f}",
            "az_max": "{:0.0f}",
            "el_min": "{:0.0f}",
            "el_max": "{:0.0f}",
            "az_margin": "{:0.1f}",
            "el_margin": "{:0.1f}",
            "max_jog_seconds": "{:0.0f}",
            "poll_interval": "{:0.1f}",
        }
        for name, values in parsed.items():
            for key, value in values.items():
                self.entries[name][key].set(formats[key].format(value))

    def _format_park_fields(self, parsed: dict[str, dict[str, float]]) -> None:
        for name, values in parsed.items():
            self.park_entries[name]["park_az"].set(f"{values['park_az']:0.0f}")
            self.park_entries[name]["park_el"].set(f"{values['park_el']:0.0f}")

    def _validate_limits(self, name: str, values: dict[str, float]) -> None:
        if not (0.0 <= values["az_min"] <= 360.0 and 0.0 <= values["az_max"] <= 360.0):
            raise RuntimeError(f"{name}: AZ limits must be 0..360 degrees.")
        if values["el_min"] >= values["el_max"]:
            raise RuntimeError(f"{name}: EL min must be less than EL max.")
        if not (0.0 <= values["el_min"] <= 90.0 and 0.0 <= values["el_max"] <= 90.0):
            raise RuntimeError(f"{name}: EL limits must be 0..90 degrees.")
        if values["az_margin"] < 0.0 or values["el_margin"] < 0.0:
            raise RuntimeError(f"{name}: margins cannot be negative.")
        if not (1.0 <= values["max_jog_seconds"] <= 600.0):
            raise RuntimeError(f"{name}: max jog must be 1..600 seconds.")
        if not (0.05 <= values["poll_interval"] <= 5.0):
            raise RuntimeError(f"{name}: poll interval must be 0.05..5.0 seconds.")

    def _validate_park(self, name: str, values: dict[str, float], limits: dict[str, float]) -> None:
        if not (0.0 <= values["park_az"] <= 360.0):
            raise RuntimeError(f"{name}: park AZ must be 0..360 degrees.")
        if not (0.0 <= values["park_el"] <= 90.0):
            raise RuntimeError(f"{name}: park EL must be 0..90 degrees.")
        test_limits = self.app.configs[name].limits
        old_values = (
            test_limits.az_min,
            test_limits.az_max,
            test_limits.el_min,
            test_limits.el_max,
        )
        try:
            test_limits.az_min = limits["az_min"]
            test_limits.az_max = limits["az_max"]
            test_limits.el_min = limits["el_min"]
            test_limits.el_max = limits["el_max"]
            test_limits.assert_position_allowed(values["park_az"], values["park_el"])
        except Exception as exc:
            raise RuntimeError(f"{name}: park position is outside limits: {exc}") from exc
        finally:
            (
                test_limits.az_min,
                test_limits.az_max,
                test_limits.el_min,
                test_limits.el_max,
            ) = old_values


class ObserverDialog(tk.Toplevel):
    def __init__(self, app: "WT4App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Observer")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.latitude_var = tk.StringVar(value=f"{app.site.latitude:0.6f}")
        self.longitude_var = tk.StringVar(value=f"{app.site.longitude:0.6f}")

        body = ttk.Frame(self, padding=10)
        body.grid(row=0, column=0, sticky="nsew")
        ttk.Label(body, text="Latitude").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(body, textvariable=self.latitude_var, width=12).grid(row=0, column=1, sticky="w", pady=2)
        ttk.Label(body, text="Longitude").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(body, textvariable=self.longitude_var, width=12).grid(row=1, column=1, sticky="w", pady=2)

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(buttons, text="Save", command=self.save).grid(row=0, column=2)

    def save(self) -> None:
        try:
            site = SiteConfig(
                latitude=float(self.latitude_var.get()),
                longitude=float(self.longitude_var.get()),
                selected_source=self.app.site.selected_source,
                track_interval_seconds=self.app.site.track_interval_seconds,
                az_track_tolerance_degrees=self.app.site.az_track_tolerance_degrees,
                el_track_tolerance_degrees=self.app.site.el_track_tolerance_degrees,
                az_stop_tolerance_degrees=self.app.site.az_stop_tolerance_degrees,
                el_stop_tolerance_degrees=self.app.site.el_stop_tolerance_degrees,
                az_slow_speed=self.app.site.az_slow_speed,
                el_slow_speed=self.app.site.el_slow_speed,
                az_slow_threshold_degrees=self.app.site.az_slow_threshold_degrees,
                el_slow_threshold_degrees=self.app.site.el_slow_threshold_degrees,
            )
            self.app.validate_observer(site)
        except ValueError:
            messagebox.showerror("Invalid Observer", "Observer location must be numeric.", parent=self)
            return
        except RuntimeError as exc:
            messagebox.showerror("Invalid Observer", str(exc), parent=self)
            return
        self.app.site = site
        self.app.save_site_settings("Observer saved.")
        self.destroy()


class SourcesDialog(tk.Toplevel):
    def __init__(self, app: "WT4App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Sources")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.sources = {name: SourceConfig(source.name, source.ra_hours, source.dec_degrees, source.flux_4800_mhz) for name, source in app.sources.items()}
        self.name_var = tk.StringVar()
        self.ra_var = tk.StringVar()
        self.dec_var = tk.StringVar()
        self.flux_var = tk.StringVar()
        self.position_after_id: Optional[str] = None
        self.protocol("WM_DELETE_WINDOW", self.close)

        body = ttk.Frame(self, padding=10)
        body.grid(row=0, column=0, sticky="nsew")
        self.tree = ttk.Treeview(body, columns=("source", "ra", "dec", "az", "el", "flux"), show="headings", height=7)
        self.tree.heading("source", text="Source")
        self.tree.heading("ra", text="RA h")
        self.tree.heading("dec", text="Dec deg")
        self.tree.heading("az", text="AZ")
        self.tree.heading("el", text="EL")
        self.tree.heading("flux", text="4800 MHz")
        self.tree.column("source", width=120, anchor="w")
        self.tree.column("ra", width=80, anchor="e")
        self.tree.column("dec", width=80, anchor="e")
        self.tree.column("az", width=70, anchor="e")
        self.tree.column("el", width=70, anchor="e")
        self.tree.column("flux", width=90, anchor="e")
        self.tree.grid(row=0, column=0, columnspan=4, sticky="nsew", pady=(0, 8))
        self.tree.bind("<<TreeviewSelect>>", self.load_selected)

        fields = ttk.Frame(body)
        fields.grid(row=1, column=0, columnspan=4, sticky="ew")
        self._field(fields, "Name", self.name_var, 0, 16)
        self._field(fields, "RA h", self.ra_var, 1, 10)
        self._field(fields, "Dec deg", self.dec_var, 2, 10)
        self._field(fields, "4800 MHz flux", self.flux_var, 3, 10)

        ttk.Button(body, text="Add/Update", command=self.add_update).grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(body, text="Remove", command=self.remove).grid(row=2, column=1, sticky="ew", pady=(8, 0), padx=(6, 0))
        ttk.Button(body, text="Select", command=self.select_source).grid(row=2, column=2, sticky="ew", pady=(8, 0), padx=(6, 0))
        ttk.Button(body, text="Close", command=self.close).grid(row=2, column=3, sticky="ew", pady=(8, 0), padx=(6, 0))
        self.refresh_tree()
        self.update_current_position()

    def _field(self, parent: ttk.Frame, label: str, variable: tk.StringVar, row: int, width: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=variable, width=width).grid(row=row, column=1, sticky="w", pady=2)

    def refresh_tree(self) -> None:
        selection = self.tree.selection()
        focus = self.tree.focus()
        self.tree.delete(*self.tree.get_children())
        for name in sorted(self.sources):
            source = self.sources[name]
            self.tree.insert("", "end", iid=name, values=self.source_row_values(source))
        if selection and selection[0] in self.sources:
            self.tree.selection_set(selection[0])
            self.tree.focus(selection[0])
        elif focus in self.sources:
            self.tree.focus(focus)
        elif self.app.site.selected_source in self.sources:
            self.tree.selection_set(self.app.site.selected_source)
            self.tree.focus(self.app.site.selected_source)

    def source_row_values(self, source: SourceConfig) -> tuple[str, str, str, str, str, str]:
        position = source_position(
            source.name,
            source.ra_hours,
            source.dec_degrees,
            self.app.site.latitude,
            self.app.site.longitude,
        )
        return (
            source.name,
            f"{source.ra_hours:0.6f}",
            f"{source.dec_degrees:0.4f}",
            f"{position.azimuth:0.2f}",
            f"{position.elevation:0.2f}",
            f"{source.flux_4800_mhz:0.1f}",
        )

    def load_selected(self, _event: Optional[object] = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        source = self.sources[selection[0]]
        self.name_var.set(source.name)
        self.ra_var.set(f"{source.ra_hours:0.6f}")
        self.dec_var.set(f"{source.dec_degrees:0.4f}")
        self.flux_var.set(f"{source.flux_4800_mhz:0.1f}")

    def update_current_position(self) -> None:
        for name, source in self.sources.items():
            if self.tree.exists(name):
                self.tree.item(name, values=self.source_row_values(source))
        self.position_after_id = self.after(1000, self.update_current_position)

    def add_update(self) -> None:
        try:
            name = self.name_var.get().strip()
            if not name:
                raise RuntimeError("Source name is required.")
            source = SourceConfig(
                name=name,
                ra_hours=float(self.ra_var.get()),
                dec_degrees=float(self.dec_var.get()),
                flux_4800_mhz=float(self.flux_var.get()),
            )
            self.validate_source(source)
        except ValueError:
            messagebox.showerror("Invalid Source", "RA, Dec, and flux must be numeric.", parent=self)
            return
        except RuntimeError as exc:
            messagebox.showerror("Invalid Source", str(exc), parent=self)
            return
        self.sources[source.name] = source
        self.refresh_tree()
        self.tree.selection_set(source.name)
        self.tree.focus(source.name)

    def remove(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        self.sources.pop(selection[0], None)
        if self.app.site.selected_source == selection[0]:
            self.app.site.selected_source = ""
        self.name_var.set("")
        self.ra_var.set("")
        self.dec_var.set("")
        self.flux_var.set("")
        self.refresh_tree()

    def select_source(self) -> None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showerror("No Source", "Select a source first.", parent=self)
            return
        self.app.site.selected_source = selection[0]
        self.save_to_app(f"Selected source {selection[0]}.")

    def close(self) -> None:
        if self.position_after_id is not None:
            self.after_cancel(self.position_after_id)
            self.position_after_id = None
        self.save_to_app("Sources saved.")
        self.destroy()

    def save_to_app(self, message: str) -> None:
        self.app.sources = self.sources
        save_sources(self.app.config_path, self.app.sources, self.app.site.selected_source)
        self.app.save_site_settings(message)

    def validate_source(self, source: SourceConfig) -> None:
        if not (0.0 <= source.ra_hours < 24.0):
            raise RuntimeError("RA must be 0.0 <= RA < 24.0 hours.")
        if not (-90.0 <= source.dec_degrees <= 90.0):
            raise RuntimeError("Dec must be -90..90 degrees.")
        if source.flux_4800_mhz < 0.0:
            raise RuntimeError("Flux cannot be negative.")


class CalibrationDialog(tk.Toplevel):
    def __init__(self, app: "WT4App") -> None:
        super().__init__(app)
        self.app = app
        self.closed = False
        self.title("Calibration")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.entries: dict[str, dict[str, tk.StringVar]] = {}
        self.tab_names: dict[str, tk.Widget] = {}
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.tabs = ttk.Notebook(self)
        self.tabs.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
        for name, panel in app.panels.items():
            frame = ttk.Frame(self.tabs, padding=10)
            self.tabs.add(frame, text=name)
            self.tab_names[name] = frame
            az_var = tk.StringVar()
            el_var = tk.StringVar()
            raw_az_var = tk.StringVar(value="--")
            raw_el_var = tk.StringVar(value="--")
            az_offset_var = tk.StringVar()
            el_offset_var = tk.StringVar()
            position = panel.session.last_position if panel.session else None
            if position:
                az_var.set(f"{position.azimuth:0.2f}")
                el_var.set(f"{position.elevation:0.2f}")
                raw_az_var.set(f"{position.raw_azimuth:0.2f}")
                raw_el_var.set(f"{position.raw_elevation:0.2f}")
            config = panel.session.config if panel.session else panel.config or app.configs.get(name)
            if config:
                az_offset_var.set(f"{config.calibration.az_offset:0.2f}")
                el_offset_var.set(f"{config.calibration.el_offset:0.2f}")
            ttk.Label(frame, text="Actual AZ").grid(row=0, column=0, sticky="w", pady=2)
            ttk.Entry(frame, textvariable=az_var, width=8).grid(row=0, column=1, sticky="w", pady=2)
            ttk.Label(frame, text="Actual EL").grid(row=1, column=0, sticky="w", pady=2)
            ttk.Entry(frame, textvariable=el_var, width=8).grid(row=1, column=1, sticky="w", pady=2)
            ttk.Separator(frame, orient="horizontal").grid(row=2, column=0, columnspan=2, sticky="ew", pady=8)
            ttk.Label(frame, text="Raw AZ").grid(row=3, column=0, sticky="w", pady=2)
            ttk.Label(frame, textvariable=raw_az_var).grid(row=3, column=1, sticky="w", pady=2)
            ttk.Label(frame, text="Raw EL").grid(row=4, column=0, sticky="w", pady=2)
            ttk.Label(frame, textvariable=raw_el_var).grid(row=4, column=1, sticky="w", pady=2)
            ttk.Separator(frame, orient="horizontal").grid(row=5, column=0, columnspan=2, sticky="ew", pady=8)
            ttk.Label(frame, text="AZ offset").grid(row=6, column=0, sticky="w", pady=2)
            ttk.Entry(frame, textvariable=az_offset_var, width=8).grid(row=6, column=1, sticky="w", pady=2)
            ttk.Label(frame, text="EL offset").grid(row=7, column=0, sticky="w", pady=2)
            ttk.Entry(frame, textvariable=el_offset_var, width=8).grid(row=7, column=1, sticky="w", pady=2)
            ttk.Button(frame, text="Calibrate Manual", command=lambda n=name: self.calibrate_manual(n)).grid(
                row=8, column=0, columnspan=2, sticky="ew", pady=(8, 0)
            )
            ttk.Button(frame, text="Calibrate From Target", command=lambda n=name: self.calibrate_from_target(n)).grid(
                row=9, column=0, columnspan=2, sticky="ew", pady=(6, 0)
            )
            ttk.Button(frame, text="Apply Offsets", command=lambda n=name: self.apply_offsets(n)).grid(
                row=10, column=0, columnspan=2, sticky="ew", pady=(6, 0)
            )
            self.entries[name] = {
                "actual_az": az_var,
                "actual_el": el_var,
                "raw_az": raw_az_var,
                "raw_el": raw_el_var,
                "az_offset": az_offset_var,
                "el_offset": el_offset_var,
            }

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Close", command=self.close).grid(row=0, column=1)
        self.refresh_live_positions()

    def refresh_live_positions(self) -> None:
        for name, panel in self.app.panels.items():
            if not panel.session:
                continue
            self.app.run_worker(
                panel.session.read_position,
                lambda position, n=name: self.app.refresh_calibration_views(n, position),
                lambda text, n=name: self.app.set_status(f"{n}: {text}"),
            )

    def select_antenna(self, name: str) -> None:
        frame = self.tab_names.get(name)
        if frame:
            self.tabs.select(frame)

    def refresh_offsets(self, name: Optional[str] = None, position: Optional[Position] = None) -> None:
        if self.closed:
            return
        names = [name] if name else list(self.entries)
        for entry_name in names:
            values = self.entries.get(entry_name)
            panel = self.app.panels.get(entry_name)
            config = panel.session.config if panel and panel.session else self.app.configs.get(entry_name)
            if not values or not config:
                continue
            values["az_offset"].set(f"{config.calibration.az_offset:0.2f}")
            values["el_offset"].set(f"{config.calibration.el_offset:0.2f}")
            panel_position = position if entry_name == name else panel.session.last_position if panel and panel.session else None
            if panel_position:
                values["actual_az"].set(f"{panel_position.azimuth:0.2f}")
                values["actual_el"].set(f"{panel_position.elevation:0.2f}")
                values["raw_az"].set(f"{panel_position.raw_azimuth:0.2f}")
                values["raw_el"].set(f"{panel_position.raw_elevation:0.2f}")

    def close(self) -> None:
        self.closed = True
        if self.app.calibration_dialog is self:
            self.app.calibration_dialog = None
        self.destroy()

    def calibrate_manual(self, name: str) -> None:
        values = self.entries[name]
        try:
            actual_az = float(values["actual_az"].get())
            actual_el = float(values["actual_el"].get())
        except ValueError:
            messagebox.showerror("Calibration", "Actual AZ and EL must be numeric.", parent=self)
            return
        self.calibrate_to_position(name, actual_az, actual_el)

    def calibrate_from_target(self, name: str) -> None:
        target = self.app.current_target
        if target is None:
            messagebox.showerror("Calibration", "No current target is available.", parent=self)
            return
        self.calibrate_to_position(name, target.azimuth, target.elevation)

    def calibrate_to_position(self, name: str, actual_az: float, actual_el: float) -> None:
        panel = self.app.panels.get(name)
        if not panel or not panel.session:
            messagebox.showerror("Calibration", f"{name} is not connected.", parent=self)
            return
        if not (0.0 <= actual_az <= 360.0 and 0.0 <= actual_el <= 90.0):
            messagebox.showerror("Calibration", "Calibration AZ must be 0..360 and EL 0..90.", parent=self)
            return

        def work() -> Position:
            position = panel.session.calibrate(actual_az, actual_el)
            self.app.save_config("Calibration saved.")
            panel.session.update_oled("CAL", activity="STOPPED")
            return position

        self.app.run_worker(
            work,
            lambda position, n=name, p=panel: self.finish_calibration(n, p, position),
            lambda text: messagebox.showerror("Calibration", text, parent=self),
        )

    def apply_offsets(self, name: str) -> None:
        panel = self.app.panels.get(name)
        config = self.app.configs.get(name)
        if config is None:
            messagebox.showerror("Calibration", f"{name} has no config.", parent=self)
            return
        values = self.entries[name]
        try:
            az_offset = float(values["az_offset"].get())
            el_offset = float(values["el_offset"].get())
        except ValueError:
            messagebox.showerror("Calibration", "Offsets must be numeric.", parent=self)
            return
        if not (-360.0 <= az_offset <= 360.0 and -90.0 <= el_offset <= 90.0):
            messagebox.showerror("Calibration", "AZ offset must be -360..360 and EL offset -90..90.", parent=self)
            return

        def work() -> Optional[Position]:
            config.calibration.az_offset = az_offset
            config.calibration.el_offset = el_offset
            self.app.save_config("Calibration offsets saved.")
            if panel and panel.session:
                with panel.session.lock:
                    position = panel.session.read_position_locked()
                    panel.session.update_oled("CAL", activity="STOPPED")
                    return position
            return None

        self.app.run_worker(
            work,
            lambda position, n=name, p=panel: self.finish_offset_apply(n, p, position),
            lambda text: messagebox.showerror("Calibration", text, parent=self),
        )

    def finish_calibration(
        self,
        name: str,
        panel: "AntennaPanel",
        position: Position,
    ) -> None:
        values = self.entries[name]
        values["actual_az"].set(f"{position.azimuth:0.2f}")
        values["actual_el"].set(f"{position.elevation:0.2f}")
        values["raw_az"].set(f"{position.raw_azimuth:0.2f}")
        values["raw_el"].set(f"{position.raw_elevation:0.2f}")
        values["az_offset"].set(f"{panel.session.config.calibration.az_offset:0.2f}")
        values["el_offset"].set(f"{panel.session.config.calibration.el_offset:0.2f}")
        panel.clear_message()
        panel.update_position(position)
        self.app.refresh_calibration_views(name, position)

    def finish_offset_apply(self, name: str, panel: Optional["AntennaPanel"], position: Optional[Position]) -> None:
        config = self.app.configs[name]
        values = self.entries[name]
        values["az_offset"].set(f"{config.calibration.az_offset:0.2f}")
        values["el_offset"].set(f"{config.calibration.el_offset:0.2f}")
        if panel and position:
            values["actual_az"].set(f"{position.azimuth:0.2f}")
            values["actual_el"].set(f"{position.elevation:0.2f}")
            values["raw_az"].set(f"{position.raw_azimuth:0.2f}")
            values["raw_el"].set(f"{position.raw_elevation:0.2f}")
            panel.clear_message()
            panel.update_position(position)
        self.app.refresh_calibration_views(name, position)


class PeakCalibrationDialog(tk.Toplevel):
    SOURCE_LABELS = ("Sun", "Moon", "Selected Source")

    def __init__(self, app: "WT4App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Peak Calibration")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.track_stop_event = threading.Event()
        self.jog_stop_event = threading.Event()
        self.tracking_axis: Optional[Axis] = None
        self.tracking_session: Optional[SafeAntenna] = None
        self.jog_thread_active = False
        self.closed = False

        connected_names = list(app.sessions) or list(app.configs)
        self.antenna_var = tk.StringVar(value=connected_names[0] if connected_names else "")
        self.source_var = tk.StringVar(value=app.default_peak_cal_source_label())
        self.status_var = tk.StringVar(value="Select source and antenna.")
        self.target_var = tk.StringVar(value="Source AZ -- EL --")
        self.position_var = tk.StringVar(value="Antenna AZ -- EL --")
        self.raw_var = tk.StringVar(value="Raw AZ -- EL --")
        self.offset_var = tk.StringVar(value="Offsets AZ -- EL --")

        body = ttk.Frame(self, padding=10)
        body.grid(row=0, column=0, sticky="nsew")

        ttk.Label(body, text="Source").grid(row=0, column=0, sticky="w", pady=2)
        source_combo = ttk.Combobox(
            body,
            textvariable=self.source_var,
            values=self.SOURCE_LABELS,
            width=18,
            state="readonly",
        )
        source_combo.grid(row=0, column=1, sticky="w", pady=2)
        ttk.Label(body, text="Antenna").grid(row=1, column=0, sticky="w", pady=2)
        antenna_combo = ttk.Combobox(
            body,
            textvariable=self.antenna_var,
            values=connected_names,
            width=18,
            state="readonly",
        )
        antenna_combo.grid(row=1, column=1, sticky="w", pady=2)
        source_combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_display(live=True))
        antenna_combo.bind("<<ComboboxSelected>>", lambda _event: self.antenna_changed())

        ttk.Separator(body, orient="horizontal").grid(row=2, column=0, columnspan=4, sticky="ew", pady=8)
        ttk.Label(body, textvariable=self.target_var).grid(row=3, column=0, columnspan=4, sticky="w", pady=2)
        ttk.Label(body, textvariable=self.position_var).grid(row=4, column=0, columnspan=4, sticky="w", pady=2)
        ttk.Label(body, textvariable=self.raw_var).grid(row=5, column=0, columnspan=4, sticky="w", pady=2)
        ttk.Label(body, textvariable=self.offset_var).grid(row=6, column=0, columnspan=4, sticky="w", pady=2)
        ttk.Label(body, textvariable=self.status_var, foreground="red").grid(row=7, column=0, columnspan=4, sticky="w", pady=(4, 0))

        tracking = ttk.LabelFrame(body, text="Axis Tracking")
        tracking.grid(row=8, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Button(tracking, text="Track AZ Only", command=lambda: self.start_axis_tracking(Axis.AZIMUTH)).grid(
            row=0, column=0, sticky="ew", padx=2, pady=2
        )
        ttk.Button(tracking, text="Track EL Only", command=lambda: self.start_axis_tracking(Axis.ELEVATION)).grid(
            row=0, column=1, sticky="ew", padx=2, pady=2
        )
        ttk.Button(tracking, text="Stop Tracking", command=self.stop_axis_tracking).grid(row=0, column=2, sticky="ew", padx=2, pady=2)

        jog = ttk.LabelFrame(body, text="Manual Peak Jog")
        jog.grid(row=9, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        for col in range(3):
            jog.columnconfigure(col, weight=1)
        self._hold_button(jog, "EL+", Direction.EL_UP).grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        self._hold_button(jog, "AZ-", Direction.AZ_CCW).grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(jog, text="STOP", command=self.stop_jog).grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        self._hold_button(jog, "AZ+", Direction.AZ_CW).grid(row=1, column=2, sticky="ew", padx=2, pady=2)
        self._hold_button(jog, "EL-", Direction.EL_DOWN).grid(row=2, column=1, sticky="ew", padx=2, pady=2)

        locks = ttk.LabelFrame(body, text="Calibration Lock")
        locks.grid(row=10, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Button(locks, text="LOCK AZ CAL", command=lambda: self.lock_axis_calibration(Axis.AZIMUTH)).grid(
            row=0, column=0, sticky="ew", padx=2, pady=2
        )
        ttk.Button(locks, text="LOCK EL CAL", command=lambda: self.lock_axis_calibration(Axis.ELEVATION)).grid(
            row=0, column=1, sticky="ew", padx=2, pady=2
        )

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Close", command=self.close).grid(row=0, column=1)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh_display(live=True)

    def _hold_button(self, master: tk.Misc, text: str, direction: Direction) -> ttk.Button:
        button = ttk.Button(master, text=text)
        button.bind("<ButtonPress-1>", lambda _event: self.start_jog(direction))
        button.bind("<ButtonRelease-1>", lambda _event: self.stop_jog())
        return button

    def source_kind(self) -> str:
        label = self.source_var.get()
        if label == "Sun":
            return "sun"
        if label == "Moon":
            return "moon"
        return "source"

    def selected_session(self) -> Optional[SafeAntenna]:
        return self.app.sessions.get(self.antenna_var.get())

    def selected_panel(self) -> Optional["AntennaPanel"]:
        return self.app.panels.get(self.antenna_var.get())

    def selected_config(self) -> Optional[AntennaConfig]:
        session = self.selected_session()
        if session:
            return session.config
        return self.app.configs.get(self.antenna_var.get())

    def current_peak_target(self) -> TargetPosition:
        return self.app.target_for_kind(self.source_kind())

    def set_source_label(self, label: str) -> None:
        if label in self.SOURCE_LABELS:
            self.source_var.set(label)
            self.refresh_display(live=True)

    def antenna_changed(self) -> None:
        self.app.select_calibration_antenna(self.antenna_var.get())
        self.refresh_display(live=True)

    def refresh_display(self, live: bool = False) -> None:
        if self.closed:
            return
        try:
            target = self.current_peak_target()
            self.target_var.set(f"{target.name} AZ {target.azimuth:0.2f} EL {target.elevation:0.2f}")
        except Exception as exc:
            self.target_var.set(f"Source unavailable: {exc}")
        session = self.selected_session()
        config = self.selected_config()
        if live and session:
            self.app.run_worker(
                session.read_position,
                lambda position, n=self.antenna_var.get(): self.finish_live_refresh(n, position),
                self.show_status,
            )
            if config:
                self.offset_var.set(
                    f"Offsets AZ {config.calibration.az_offset:+0.2f} EL {config.calibration.el_offset:+0.2f}"
                )
            self.after(1000, self.refresh_display)
            return
        position = session.last_position if session else None
        if position:
            self.position_var.set(f"Antenna AZ {position.azimuth:0.2f} EL {position.elevation:0.2f}")
            self.raw_var.set(f"Raw AZ {position.raw_azimuth:0.2f} EL {position.raw_elevation:0.2f}")
        else:
            self.position_var.set("Antenna AZ -- EL --")
            self.raw_var.set("Raw AZ -- EL --")
        if config:
            self.offset_var.set(
                f"Offsets AZ {config.calibration.az_offset:+0.2f} EL {config.calibration.el_offset:+0.2f}"
            )
        self.after(1000, self.refresh_display)

    def finish_live_refresh(self, name: str, position: Position) -> None:
        if self.closed or name != self.antenna_var.get():
            return
        panel = self.selected_panel()
        if panel:
            panel.update_position(position)
        self.app.refresh_calibration_views(name, position)
        self.status_var.set("Ready.")

    def refresh_offsets(self, name: Optional[str] = None, position: Optional[Position] = None) -> None:
        if self.closed:
            return
        selected_name = self.antenna_var.get()
        if name is not None and name != selected_name:
            return
        config = self.selected_config()
        if config:
            self.offset_var.set(
                f"Offsets AZ {config.calibration.az_offset:+0.2f} EL {config.calibration.el_offset:+0.2f}"
            )
        if position:
            self.position_var.set(f"Antenna AZ {position.azimuth:0.2f} EL {position.elevation:0.2f}")
            self.raw_var.set(f"Raw AZ {position.raw_azimuth:0.2f} EL {position.raw_elevation:0.2f}")

    def start_axis_tracking(self, axis: Axis) -> None:
        session = self.selected_session()
        if session is None:
            self.status_var.set("Connect the selected antenna first.")
            return
        try:
            self.app.prepare_peak_calibration_owner()
        except Exception as exc:
            self.status_var.set(str(exc))
            return
        try:
            self.app.validate_site(self.app.site)
            self.current_peak_target()
        except Exception as exc:
            self.status_var.set(str(exc))
            return
        if self.tracking_axis is not None:
            self.status_var.set("Stop current Peak Cal tracking before starting another axis.")
            return
        self.track_stop_event = threading.Event()
        self.tracking_axis = axis
        self.tracking_session = session
        threading.Thread(target=self.axis_tracking_loop, args=(session, axis, self.track_stop_event), daemon=True).start()
        self.status_var.set(f"Tracking {axis_label(axis)} only. Manually peak the other axis.")

    def axis_tracking_loop(self, session: SafeAntenna, axis: Axis, stop_event: threading.Event) -> None:
        panel = self.selected_panel()
        try:
            while not stop_event.is_set():
                target = self.current_peak_target()
                target_value = target.azimuth if axis == Axis.AZIMUTH else target.elevation
                self.app.events.put(("ok", self.app.apply_target_position, target))
                if panel:
                    self.app.events.put(("ok", panel.set_tracking_status, f"CAL {axis_label(axis)}"))

                def progress(position: Position) -> None:
                    if panel:
                        self.app.events.put(("position", panel.update_position, position))
                    session.update_oled_position(target.azimuth, target.elevation, f"CAL {axis_label(axis)}")

                position = session.guarded_slew_axis_to(
                    axis,
                    target_value,
                    session.config.az_track_speed if axis == Axis.AZIMUTH else session.config.el_track_speed,
                    stop_event,
                    self.app.az_tracking_start_tolerance() if axis == Axis.AZIMUTH else self.app.el_tracking_start_tolerance(),
                    self.app.az_tracking_stop_tolerance() if axis == Axis.AZIMUTH else self.app.el_tracking_stop_tolerance(),
                    self.app.site.az_slow_speed if axis == Axis.AZIMUTH else self.app.site.el_slow_speed,
                    self.app.site.az_slow_threshold_degrees if axis == Axis.AZIMUTH else self.app.site.el_slow_threshold_degrees,
                    progress,
                )
                if panel:
                    self.app.events.put(("position", panel.update_position, position))
                session.update_oled(target.name[:8].upper(), target.azimuth, target.elevation, f"CAL {axis_label(axis)}")
                wait_until = time.monotonic() + max(0.1, self.app.site.track_interval_seconds)
                while not stop_event.is_set() and time.monotonic() < wait_until:
                    time.sleep(0.05)
        except Exception as exc:
            self.app.events.put(("error", self.show_status, str(exc)))
        finally:
            if panel:
                self.app.events.put(("ok", panel.set_tracking_status, "STOPPED"))

    def stop_axis_tracking(self) -> None:
        self.track_stop_event.set()
        session = self.tracking_session
        if session and self.tracking_axis is not None:
            self.app.run_worker(lambda s=session: (s.stop_all(), s.update_oled_activity("STOPPED")), lambda _result: None, self.show_status)
        self.tracking_axis = None
        self.tracking_session = None
        self.status_var.set("Peak calibration tracking stopped.")

    def start_jog(self, direction: Direction) -> None:
        session = self.selected_session()
        if session is None or self.jog_thread_active:
            return
        try:
            self.app.prepare_peak_calibration_owner()
        except Exception as exc:
            self.status_var.set(str(exc))
            return
        jog_axis = Axis.AZIMUTH if direction in (Direction.AZ_CW, Direction.AZ_CCW) else Axis.ELEVATION
        if self.tracking_axis == jog_axis:
            self.status_var.set(f"Stop {axis_label(jog_axis)} tracking before jogging that axis.")
            return
        self.jog_stop_event.clear()
        self.jog_thread_active = True
        panel = self.selected_panel()
        speed = panel.speed_value if panel else session.config.gui_speed

        def progress(position: Position) -> None:
            if panel:
                self.app.events.put(("position", panel.update_position, position))
            session.update_oled_position(activity=f"PEAK {axis_label(jog_axis)}")

        def work() -> Position:
            session.update_oled("PEAK", activity=f"PEAK {axis_label(jog_axis)}")
            session.guarded_jog(direction, speed, None, self.jog_stop_event, progress)
            position = session.read_position()
            session.update_oled_activity("STOPPED")
            return position

        self.app.run_worker(work, self.finish_jog, self.finish_jog_fault)

    def stop_jog(self) -> None:
        self.jog_stop_event.set()

    def finish_jog(self, position: Position) -> None:
        self.jog_thread_active = False
        panel = self.selected_panel()
        if panel:
            panel.update_position(position)
        self.status_var.set("Peak jog ready.")

    def finish_jog_fault(self, text: str) -> None:
        self.jog_thread_active = False
        self.status_var.set(text)

    def lock_axis_calibration(self, axis: Axis) -> None:
        session = self.selected_session()
        if session is None:
            self.status_var.set("Connect the selected antenna first.")
            return
        try:
            self.app.prepare_peak_calibration_owner()
        except Exception as exc:
            self.status_var.set(str(exc))
            return
        if self.tracking_axis == axis:
            self.status_var.set(f"Track the other axis before locking {axis_label(axis)} calibration.")
            return

        def work() -> tuple[Position, TargetPosition, float, float]:
            target = self.current_peak_target()
            actual = target.azimuth if axis == Axis.AZIMUTH else target.elevation
            old_offset = (
                session.config.calibration.az_offset if axis == Axis.AZIMUTH else session.config.calibration.el_offset
            )
            position = session.calibrate_axis(axis, actual)
            self.app.save_config("Peak calibration saved.")
            session.update_oled(target.name[:8].upper(), target.azimuth, target.elevation, f"CAL {axis_label(axis)}")
            new_offset = (
                session.config.calibration.az_offset if axis == Axis.AZIMUTH else session.config.calibration.el_offset
            )
            return position, target, old_offset, new_offset

        self.app.run_worker(
            work,
            lambda result, a=axis: self.finish_axis_lock(a, result),
            self.show_status,
        )

    def finish_axis_lock(self, axis: Axis, result: tuple[Position, TargetPosition, float, float]) -> None:
        position, target, old_offset, new_offset = result
        panel = self.selected_panel()
        if panel:
            panel.update_position(position)
            panel.clear_message()
        self.app.refresh_calibration_views(self.antenna_var.get(), position)
        self.status_var.set(
            f"Locked {axis_label(axis)} to {target.name}: offset {old_offset:+0.2f} -> {new_offset:+0.2f}."
        )

    def show_status(self, text: str) -> None:
        self.status_var.set(text)

    def close(self) -> None:
        self.closed = True
        self.track_stop_event.set()
        self.jog_stop_event.set()
        if self.app.peak_calibration_dialog is self:
            self.app.peak_calibration_dialog = None
        self.destroy()


class EncodersDialog(tk.Toplevel):
    COLUMNS = (
        "Antenna",
        "Axis",
        "Addr",
        "Type",
        "Model",
        "Version",
        "Config",
        "Serial",
        "Date",
        "Resolution",
        "Position",
        "Mode",
    )

    def __init__(self, app: "WT4App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Encoders")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.position_vars: dict[tuple[str, Axis], tk.StringVar] = {}
        self.row_widgets: list[tk.Widget] = []

        self.body = ttk.Frame(self, padding=10)
        self.body.grid(row=0, column=0, sticky="nsew")
        for column, title in enumerate(self.COLUMNS):
            ttk.Label(self.body, text=title, font=("TkDefaultFont", 9, "bold")).grid(
                row=0, column=column, sticky="w", padx=3, pady=(0, 4)
            )

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Scan", command=self.scan).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(buttons, text="Close", command=self.destroy).grid(row=0, column=2)
        self.scan()

    def scan(self) -> None:
        if not self.app.sessions:
            self.show_error("Connect antennas before encoder scan.")
            return

        def work() -> dict[str, dict[Axis, EncoderInfo]]:
            return {name: session.scan_encoders() for name, session in self.app.sessions.items()}

        self.app.run_worker(work, self.finish_scan, self.show_error)

    def finish_scan(self, result: dict[str, dict[Axis, EncoderInfo]]) -> None:
        for widget in self.row_widgets:
            widget.destroy()
        self.row_widgets.clear()
        self.position_vars.clear()
        row = 1
        for name, axes in result.items():
            for axis in (Axis.AZIMUTH, Axis.ELEVATION):
                info = axes[axis]
                self.add_row(row, name, info)
                row += 1

    def add_row(self, row: int, name: str, info: EncoderInfo) -> None:
        values = (
            name,
            "AZ" if info.axis == Axis.AZIMUTH else "EL",
            str(info.address),
            info.encoder_type,
            str(info.model),
            info.version,
            info.config,
            str(info.serial),
            info.date,
            str(info.resolution),
            f"{info.position:0.2f}",
            str(info.mode),
        )
        for column, value in enumerate(values):
            if self.COLUMNS[column] == "Position":
                var = tk.StringVar(value=value)
                entry = ttk.Entry(self.body, textvariable=var, width=8)
                entry.grid(row=row, column=column, sticky="ew", padx=3, pady=2)
                self.position_vars[(name, info.axis)] = var
                self.row_widgets.append(entry)
            else:
                label = ttk.Label(self.body, text=value)
                label.grid(row=row, column=column, sticky="w", padx=3, pady=2)
                self.row_widgets.append(label)
        button = ttk.Button(self.body, text="Set", command=lambda n=name, a=info.axis: self.set_position(n, a))
        button.grid(row=row, column=len(self.COLUMNS), sticky="ew", padx=3, pady=2)
        self.row_widgets.append(button)

    def set_position(self, name: str, axis: Axis) -> None:
        panel = self.app.panels.get(name)
        session = self.app.sessions.get(name)
        if panel is None or session is None:
            self.show_error(f"{name} is not connected.")
            return
        try:
            position = float(self.position_vars[(name, axis)].get())
        except ValueError:
            self.show_error("Position must be numeric.")
            return
        axis_label = "AZ" if axis == Axis.AZIMUTH else "EL"
        if axis == Axis.AZIMUTH and not (0.0 <= position <= 360.0):
            self.show_error("AZ position must be 0..360 degrees.")
            return
        if axis == Axis.ELEVATION and not (0.0 <= position <= 90.0):
            self.show_error("EL Arduino position must be 0..90 degrees.")
            return
        if not messagebox.askyesno(
            "Set Encoder Position",
            f"Set {name} {axis_label} Arduino position to {position:0.2f}?\n\n"
            "This resets the WT4 software calibration offset for this axis to zero.",
            parent=self,
        ):
            return

        def work() -> Position:
            updated = session.set_encoder_position(axis, position)
            self.app.save_config("Encoder position saved.")
            session.update_oled("CAL", activity="STOPPED")
            return updated

        self.app.run_worker(
            work,
            lambda updated, p=panel: self.finish_set(p, updated),
            self.show_error,
        )

    def finish_set(self, panel: "AntennaPanel", position: Position) -> None:
        panel.clear_message()
        panel.update_position(position)
        self.scan()

    def show_error(self, text: str) -> None:
        messagebox.showerror("Encoders", text, parent=self)


class TrackingDialog(tk.Toplevel):
    def __init__(self, app: "WT4App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Tracking")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.az_speed_vars: dict[str, tk.StringVar] = {}
        self.el_speed_vars: dict[str, tk.StringVar] = {}
        self.max_jog_vars: dict[str, tk.StringVar] = {}
        self.interval_var = tk.StringVar(value=f"{app.site.track_interval_seconds:0.1f}")
        self.az_tolerance_var = tk.StringVar(value=f"{app.site.az_track_tolerance_degrees:0.2f}")
        self.el_tolerance_var = tk.StringVar(value=f"{app.site.el_track_tolerance_degrees:0.2f}")
        self.az_stop_tolerance_var = tk.StringVar(value=f"{app.site.az_stop_tolerance_degrees:0.2f}")
        self.el_stop_tolerance_var = tk.StringVar(value=f"{app.site.el_stop_tolerance_degrees:0.2f}")
        self.az_slow_speed_var = tk.StringVar(value=str(app.site.az_slow_speed))
        self.el_slow_speed_var = tk.StringVar(value=str(app.site.el_slow_speed))
        self.az_slow_threshold_var = tk.StringVar(value=f"{app.site.az_slow_threshold_degrees:0.1f}")
        self.el_slow_threshold_var = tk.StringVar(value=f"{app.site.el_slow_threshold_degrees:0.1f}")

        body = ttk.Frame(self, padding=10)
        body.grid(row=0, column=0, sticky="nsew")
        self._spin_field(body, "Interval sec", self.interval_var, 0, 0.1, 10.0, 0.1, width=7)

        ttk.Separator(body, orient="horizontal").grid(row=1, column=0, columnspan=5, sticky="ew", pady=8)
        ttk.Label(body, text="Axis").grid(row=2, column=0, sticky="w")
        ttk.Label(body, text="Start tol").grid(row=2, column=1, sticky="w")
        ttk.Label(body, text="Stop tol").grid(row=2, column=2, sticky="w")
        ttk.Label(body, text="Slow speed").grid(row=2, column=3, sticky="w")
        ttk.Label(body, text="Slow deg").grid(row=2, column=4, sticky="w")
        ttk.Label(body, text="AZ").grid(row=3, column=0, sticky="w", pady=2)
        self._spin_only(body, self.az_tolerance_var, 3, 1, -0.20, 0.20, 0.01, width=7)
        self._spin_only(body, self.az_stop_tolerance_var, 3, 2, -0.20, 0.20, 0.01, width=7)
        ttk.Entry(body, textvariable=self.az_slow_speed_var, width=7).grid(row=3, column=3, sticky="w", pady=2)
        ttk.Entry(body, textvariable=self.az_slow_threshold_var, width=7).grid(row=3, column=4, sticky="w", pady=2)
        ttk.Label(body, text="EL").grid(row=4, column=0, sticky="w", pady=2)
        self._spin_only(body, self.el_tolerance_var, 4, 1, -0.20, 0.20, 0.01, width=7)
        self._spin_only(body, self.el_stop_tolerance_var, 4, 2, -0.20, 0.20, 0.01, width=7)
        ttk.Entry(body, textvariable=self.el_slow_speed_var, width=7).grid(row=4, column=3, sticky="w", pady=2)
        ttk.Entry(body, textvariable=self.el_slow_threshold_var, width=7).grid(row=4, column=4, sticky="w", pady=2)

        ttk.Separator(body, orient="horizontal").grid(row=5, column=0, columnspan=5, sticky="ew", pady=8)
        ttk.Label(body, text="Antenna").grid(row=6, column=0, sticky="w")
        ttk.Label(body, text="AZ speed").grid(row=6, column=1, sticky="w")
        ttk.Label(body, text="EL speed").grid(row=6, column=2, sticky="w")
        ttk.Label(body, text="Max jog").grid(row=6, column=3, sticky="w")
        for row, (name, config) in enumerate(self.app.configs.items(), start=7):
            self.az_speed_vars[name] = tk.StringVar(value=str(config.az_track_speed))
            self.el_speed_vars[name] = tk.StringVar(value=str(config.el_track_speed))
            self.max_jog_vars[name] = tk.StringVar(value=f"{config.limits.max_jog_seconds:0.0f}")
            ttk.Label(body, text=name).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(body, textvariable=self.az_speed_vars[name], width=7).grid(row=row, column=1, sticky="w", pady=2)
            ttk.Entry(body, textvariable=self.el_speed_vars[name], width=7).grid(row=row, column=2, sticky="w", pady=2)
            ttk.Entry(body, textvariable=self.max_jog_vars[name], width=7).grid(row=row, column=3, sticky="w", pady=2)

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(buttons, text="Save", command=self.save).grid(row=0, column=2)

    def _field(self, parent: ttk.Frame, label: str, variable: tk.StringVar, row: int, width: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=variable, width=width).grid(row=row, column=1, sticky="w", pady=2)

    def _spin_field(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        row: int,
        from_value: float,
        to_value: float,
        increment: float,
        width: int,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        tk.Spinbox(
            parent,
            textvariable=variable,
            from_=from_value,
            to=to_value,
            increment=increment,
            width=width,
            format="%0.2f" if increment < 0.1 else "%0.1f",
        ).grid(row=row, column=1, sticky="w", pady=2)

    def _spin_only(
        self,
        parent: ttk.Frame,
        variable: tk.StringVar,
        row: int,
        column: int,
        from_value: float,
        to_value: float,
        increment: float,
        width: int,
    ) -> None:
        tk.Spinbox(
            parent,
            textvariable=variable,
            from_=from_value,
            to=to_value,
            increment=increment,
            width=width,
            format="%0.2f" if increment < 0.1 else "%0.1f",
        ).grid(row=row, column=column, sticky="w", pady=2)

    def save(self) -> None:
        try:
            site = SiteConfig(
                latitude=self.app.site.latitude,
                longitude=self.app.site.longitude,
                selected_source=self.app.site.selected_source,
                track_interval_seconds=round(float(self.interval_var.get()), 1),
                az_track_tolerance_degrees=round(float(self.az_tolerance_var.get()), 2),
                el_track_tolerance_degrees=round(float(self.el_tolerance_var.get()), 2),
                az_stop_tolerance_degrees=round(float(self.az_stop_tolerance_var.get()), 2),
                el_stop_tolerance_degrees=round(float(self.el_stop_tolerance_var.get()), 2),
                az_slow_speed=int(self.az_slow_speed_var.get()),
                el_slow_speed=int(self.el_slow_speed_var.get()),
                az_slow_threshold_degrees=round(float(self.az_slow_threshold_var.get()), 1),
                el_slow_threshold_degrees=round(float(self.el_slow_threshold_var.get()), 1),
            )
            self.app.validate_site(site)
            antenna_values = {
                name: (
                    int(self.az_speed_vars[name].get()),
                    int(self.el_speed_vars[name].get()),
                    float(self.max_jog_vars[name].get()),
                )
                for name in self.app.configs
            }
            self._validate_antennas(antenna_values, site)
        except ValueError:
            messagebox.showerror("Invalid Tracking", "Tracking values must be numeric.", parent=self)
            return
        except RuntimeError as exc:
            messagebox.showerror("Invalid Tracking", str(exc), parent=self)
            return

        self.app.site = site
        for name, (az_speed, el_speed, max_jog) in antenna_values.items():
            config = self.app.configs[name]
            config.az_track_speed = az_speed
            config.el_track_speed = el_speed
            config.limits.max_jog_seconds = max_jog
            if name in self.app.panels:
                self.app.panels[name].sync_config_settings()
        self.app.save_tracking_and_config("Tracking saved.")
        self.destroy()

    def _validate_antennas(self, values: dict[str, tuple[int, int, float]], site: SiteConfig) -> None:
        for name, (az_speed, el_speed, max_jog) in values.items():
            if not (1 <= az_speed <= 100 and 1 <= el_speed <= 100):
                raise RuntimeError(f"{name}: AZ and EL speeds must be 1..100.")
            if site.az_slow_speed >= az_speed:
                raise RuntimeError(f"{name}: AZ slow speed must be lower than AZ speed.")
            if site.el_slow_speed >= el_speed:
                raise RuntimeError(f"{name}: EL slow speed must be lower than EL speed.")
            if not (1.0 <= max_jog <= 600.0):
                raise RuntimeError(f"{name}: max jog must be 1..600 seconds.")


class AntennaPanel(ttk.Frame):
    def __init__(self, master: tk.Misc, app: "WT4App", name: str, config: Optional[AntennaConfig] = None) -> None:
        super().__init__(master, padding=8)
        self.app = app
        self.name = name
        self.config = config
        self.session: Optional[SafeAntenna] = None
        self.stop_event = threading.Event()

        self.status_var = tk.StringVar(value="DISCONNECTED")
        self.cal_az_var = tk.StringVar(value="--")
        self.cal_el_var = tk.StringVar(value="--")
        self.fault_var = tk.StringVar(value="")

        initial_speed = config.gui_speed if config else 40
        initial_max_jog = config.limits.max_jog_seconds if config else 60.0
        self.speed_value = initial_speed
        self.max_jog_value = initial_max_jog
        self.speed_var = tk.StringVar(value=str(initial_speed))
        self.max_jog_var = tk.StringVar(value=f"{initial_max_jog:0.1f}")
        self.jog_thread_active = False

        self.columnconfigure(1, weight=1)
        ttk.Label(self, text=name.upper(), font=("TkDefaultFont", 13, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(self, textvariable=self.status_var).grid(row=0, column=1, sticky="e")

        position_frame = ttk.Frame(self)
        position_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        for col in range(2):
            position_frame.columnconfigure(col, weight=1)
        self._position_cell(position_frame, 0, 0, "AZ", self.cal_az_var)
        self._position_cell(position_frame, 1, 0, "EL", self.cal_el_var)

        control = ttk.LabelFrame(self, text="Manual")
        control.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        for col in range(3):
            control.columnconfigure(col, weight=1)
        self._hold_button(control, "EL+", Direction.EL_UP).grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        self._hold_button(control, "AZ-", Direction.AZ_CCW).grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(control, text="STOP", command=self.stop).grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        self._hold_button(control, "AZ+", Direction.AZ_CW).grid(row=1, column=2, sticky="ew", padx=2, pady=2)
        self._hold_button(control, "EL-", Direction.EL_DOWN).grid(row=2, column=1, sticky="ew", padx=2, pady=2)

        self.reference_frame: Optional[ttk.Frame] = None
        ttk.Label(self, textvariable=self.fault_var, foreground="red", wraplength=260).grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0)
        )

    def _position_cell(self, parent: tk.Misc, row: int, column: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=(0, 4))
        ttk.Label(parent, textvariable=variable, font=("TkDefaultFont", 13)).grid(
            row=row, column=column + 1, sticky="e", padx=(0, 8)
        )

    def add_reference_block(
        self,
        sun_var: tk.StringVar,
        moon_var: tk.StringVar,
        local_time_var: tk.StringVar,
        lmst_var: tk.StringVar,
        utc_var: tk.StringVar,
    ) -> None:
        self.reference_frame = ttk.Frame(self)
        self.reference_frame.grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(self.reference_frame, textvariable=sun_var).grid(row=0, column=0, sticky="w")
        ttk.Label(self.reference_frame, textvariable=moon_var).grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Label(self.reference_frame, textvariable=local_time_var).grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Label(self.reference_frame, textvariable=lmst_var).grid(row=4, column=0, sticky="w", pady=(2, 0))
        ttk.Label(self.reference_frame, textvariable=utc_var).grid(row=5, column=0, sticky="w", pady=(2, 0))

    def _hold_button(self, master: tk.Misc, text: str, direction: Direction) -> ttk.Button:
        button = ttk.Button(master, text=text)
        button.bind("<ButtonPress-1>", lambda _event: self.start_jog(direction))
        button.bind("<ButtonRelease-1>", lambda _event: self.stop())
        button.bind("<Leave>", lambda _event: self.stop())
        return button

    def attach(self, session: SafeAntenna) -> None:
        self.session = session
        self.sync_config_settings()
        self.status_var.set("STOPPED")
        self.fault_var.set("")
        self.update_position(session.last_position)

    def detach(self) -> None:
        self.stop_event.set()
        self.session = None
        self.jog_thread_active = False
        self.status_var.set("DISCONNECTED")
        self.fault_var.set("")
        self.clear_position_fields()

    def clear_position_fields(self) -> None:
        self.cal_az_var.set("--")
        self.cal_el_var.set("--")

    def sync_config_settings(self) -> None:
        config = self.session.config if self.session else self.config
        if not config:
            return
        self.speed_value = config.gui_speed
        self.max_jog_value = config.limits.max_jog_seconds
        self.speed_var.set(str(self.speed_value))
        self.max_jog_var.set(f"{self.max_jog_value:0.1f}")

    def update_position(self, position: Optional[Position]) -> None:
        if position is None:
            return
        self.cal_az_var.set(f"{position.azimuth:0.2f}")
        self.cal_el_var.set(f"{position.elevation:0.2f}")

    def set_fault(self, text: str) -> None:
        self.fault_var.set(text)
        self.status_var.set("FAULT" if text else "STOPPED")

    def set_tracking_status(self, text: str) -> None:
        if self.session and not self.fault_var.get():
            self.status_var.set(text)

    def set_message(self, text: str) -> None:
        self.fault_var.set(text)
        if self.session:
            self.status_var.set("STOPPED")

    def clear_message(self) -> None:
        self.fault_var.set("")
        if self.session:
            self.status_var.set("STOPPED")

    def refresh(self) -> None:
        if not self.session:
            return
        self.app.run_worker(lambda: self.session.read_position(), self.finish_refresh, self.set_message)

    def commit_speed(self, _event: Optional[object] = None) -> bool:
        try:
            value = max(0, min(100, int(self.speed_var.get())))
        except ValueError:
            self.speed_var.set(str(self.speed_value))
            self.set_message("Speed must be a whole number from 0 to 100.")
            return False
        self.speed_value = value
        self.speed_var.set(str(value))
        config = self.session.config if self.session else self.config
        if config:
            config.gui_speed = value
            self.app.save_config("Settings saved.")
        self.clear_message()
        return True

    def commit_max_jog(self, _event: Optional[object] = None) -> bool:
        try:
            value = max(1.0, min(600.0, float(self.max_jog_var.get())))
        except ValueError:
            self.max_jog_var.set(f"{self.max_jog_value:0.1f}")
            self.set_message("Max jog must be a number from 1 to 600 seconds.")
            return False
        self.max_jog_value = value
        self.max_jog_var.set(f"{value:0.1f}")
        config = self.session.config if self.session else self.config
        if config:
            config.limits.max_jog_seconds = value
            self.app.save_config("Settings saved.")
        self.clear_message()
        return True

    def start_jog(self, direction: Direction) -> None:
        if not self.session or self.jog_thread_active:
            return
        session = self.session
        self.stop_event.clear()
        speed = self.speed_value
        self.jog_thread_active = True

        def realtime_update(position: Position) -> None:
            self.queue_position_update(position)
            session.update_oled_position(activity="JOG")

        def work() -> Position:
            session.update_oled("MANUAL", activity="JOG")
            session.guarded_jog(direction, speed, None, self.stop_event, realtime_update)
            position = session.read_position()
            session.update_oled("MANUAL", activity="STOPPED")
            return position

        self.app.run_worker(work, self.finish_jog, self.finish_jog_fault)

    def queue_position_update(self, position: Position) -> None:
        self.app.events.put(("position", self.update_position, position))

    def finish_refresh(self, position: Position) -> None:
        self.clear_message()
        self.update_position(position)

    def finish_jog(self, position: Position) -> None:
        self.jog_thread_active = False
        self.clear_message()
        self.update_position(position)

    def finish_jog_fault(self, text: str) -> None:
        self.jog_thread_active = False
        self.set_message(text)

    def stop(self) -> None:
        self.stop_event.set()
        if self.session:
            self.app.run_worker(lambda: self.session.stop_all(), lambda _result: None, self.set_fault)


class PowerMeterPanel(ttk.LabelFrame):
    def __init__(self, master: tk.Misc, app: "WT4App") -> None:
        super().__init__(master, text="RTL Power Meter", padding=8)
        self.app = app
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.meter: Optional[RtlPowerMeter] = None
        self.power_values: list[float] = []
        self.last_reading_time = 0.0
        self.latest_power_dbfs: Optional[float] = None
        self.power_started_at = 0.0
        self.warmup_seconds = 0.0
        self.history_values: list[float] = []
        self.log_handle = None
        self.log_writer: Optional[csv.writer] = None
        self.log_path: Optional[Path] = None

        power = app.power_config
        self.freq_var = tk.StringVar(value=f"{power.center_frequency_hz / 1_000_000:0.1f}")
        self.rate_var = tk.StringVar(value=f"{power.sample_rate_hz / 1000:0.0f}")
        self.gain_var = tk.StringVar(value=power.gain_db)
        self.samples_var = tk.StringVar(value=self.samples_display_value(power.samples_per_read))
        self.update_var = tk.StringVar(value=f"{power.update_rate_hz:0.0f}")
        self.smooth_var = tk.StringVar(value=str(power.smoothing_samples))
        self.warmup_var = tk.StringVar(value=f"{power.warmup_seconds:0.0f}")
        self.power_var = tk.StringVar(value="--.- dBFS")
        self.status_var = tk.StringVar(value="Stopped")
        self.stats_var = tk.StringVar(value="Avg -- Min -- Max --")

        fields = ttk.Frame(self)
        fields.grid(row=0, column=0, sticky="ew")
        for column in range(14):
            fields.columnconfigure(column, weight=1 if column % 2 else 0)
        self._entry(fields, "Freq MHz", self.freq_var, 0, width=7)
        self._entry(fields, "Sample ksps", self.rate_var, 2, width=6)
        self._entry(fields, "Gain", self.gain_var, 4, width=6)
        self._entry(fields, "Samples k", self.samples_var, 6, width=7)
        self._entry(fields, "GUI Hz", self.update_var, 8, width=5)
        self._entry(fields, "Avg", self.smooth_var, 10, width=4)
        self._entry(fields, "Warm s", self.warmup_var, 12, width=5)

        controls = ttk.Frame(self)
        controls.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(controls, text="Start Power", command=self.start).pack(side="left")
        ttk.Button(controls, text="Stop Power", command=self.stop).pack(side="left", padx=(6, 0))
        ttk.Button(controls, text="Start Log", command=self.start_log).pack(side="left", padx=(6, 0))
        ttk.Button(controls, text="Stop Log", command=self.stop_log).pack(side="left", padx=(6, 0))
        ttk.Label(controls, textvariable=self.power_var, font=("TkDefaultFont", 13)).pack(side="left", padx=(18, 0))
        ttk.Label(controls, textvariable=self.status_var).pack(side="left", padx=(14, 0))
        ttk.Label(controls, textvariable=self.stats_var).pack(side="left", padx=(14, 0))

    def _entry(self, parent: tk.Misc, label: str, variable: tk.StringVar, column: int, width: int) -> None:
        ttk.Label(parent, text=label).grid(row=0, column=column, sticky="w", padx=(0, 2))
        ttk.Entry(parent, textvariable=variable, width=width).grid(row=0, column=column + 1, sticky="w", padx=(0, 8))

    def samples_display_value(self, stored_value: str) -> str:
        text = stored_value.strip().lower()
        if text in ("", "auto", "0"):
            return "auto"
        try:
            return f"{int(round(int(text) / 1000)):d}"
        except ValueError:
            return stored_value

    def samples_stored_value(self) -> str:
        text = self.samples_var.get().strip().lower()
        if text in ("", "auto", "0"):
            return "auto"
        return str(int(round(float(text) * 1000)))

    def power_config_from_fields(self) -> PowerConfig:
        freq_hz = int(round(float(self.freq_var.get()) * 1_000_000))
        sample_rate_hz = int(round(float(self.rate_var.get()) * 1000))
        return PowerConfig(
            center_frequency_hz=freq_hz,
            sample_rate_hz=sample_rate_hz,
            gain_db=self.gain_var.get().strip() or "auto",
            samples_per_read=self.samples_stored_value(),
            update_rate_hz=float(self.update_var.get()),
            smoothing_samples=max(1, int(self.smooth_var.get())),
            warmup_seconds=max(0.0, float(self.warmup_var.get())),
        )

    def meter_config_from_fields(self) -> PowerMeterConfig:
        power = self.power_config_from_fields()
        gain_text = self.gain_var.get().strip().lower()
        gain = None if gain_text in ("", "auto") else float(gain_text)
        samples_text = power.samples_per_read.strip().lower()
        samples = None if samples_text in ("", "auto", "0") else int(samples_text)
        config = PowerMeterConfig(
            center_frequency_hz=power.center_frequency_hz,
            sample_rate_hz=power.sample_rate_hz,
            measurement_bandwidth_hz=power.sample_rate_hz,
            update_rate_hz=power.update_rate_hz,
            gain_db=gain,
            smoothing_samples=power.smoothing_samples,
            samples_per_read=samples,
        )
        config.validate()
        return config

    def save_settings(self) -> None:
        try:
            self.app.power_config = self.power_config_from_fields()
        except Exception:
            return
        save_power_config(self.app.config_path, self.app.power_config)

    def format_fields(self, power: PowerConfig) -> None:
        self.freq_var.set(f"{power.center_frequency_hz / 1_000_000:0.1f}")
        self.rate_var.set(f"{power.sample_rate_hz / 1000:0.0f}")
        self.samples_var.set(self.samples_display_value(power.samples_per_read))
        self.update_var.set(f"{power.update_rate_hz:0.0f}")
        self.smooth_var.set(str(power.smoothing_samples))
        self.warmup_var.set(f"{power.warmup_seconds:0.0f}")

    def start_log(self) -> None:
        if self.log_writer:
            self.status_var.set(f"Logging {self.log_path.name if self.log_path else ''}".strip())
            return
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log_path = Path(f"wt4_power_{timestamp}.csv")
        self.log_handle = self.log_path.open("w", newline="", encoding="utf-8")
        self.log_writer = csv.writer(self.log_handle)
        self.log_writer.writerow(self.log_header())
        self.status_var.set(f"Logging {self.log_path.name}")

    def stop_log(self) -> None:
        if self.log_handle:
            self.log_handle.close()
        self.log_handle = None
        self.log_writer = None
        self.log_path = None
        if not (self.thread and self.thread.is_alive()):
            self.status_var.set("Stopped")

    def log_header(self) -> list[str]:
        header = [
            "local_time",
            "utc_time",
            "power_dbfs",
            "target_name",
            "target_az",
            "target_el",
        ]
        for name in self.app.panels:
            header.extend([f"{name}_az", f"{name}_el", f"{name}_raw_az", f"{name}_raw_el"])
        return header

    def log_reading(self, power_dbfs: float) -> None:
        if not self.log_writer:
            return
        now_local = datetime.now().astimezone()
        now_utc = datetime.now(timezone.utc)
        target = self.app.current_target
        row: list[object] = [
            now_local.isoformat(timespec="milliseconds"),
            now_utc.isoformat(timespec="milliseconds"),
            f"{power_dbfs:0.2f}",
            self.app.target_name_var.get().replace("Target ", ""),
            f"{target.azimuth:0.3f}" if target else "",
            f"{target.elevation:0.3f}" if target else "",
        ]
        for panel in self.app.panels.values():
            position = panel.session.last_position if panel.session else None
            if position:
                row.extend(
                    [
                        f"{position.azimuth:0.3f}",
                        f"{position.elevation:0.3f}",
                        f"{position.raw_azimuth:0.3f}",
                        f"{position.raw_elevation:0.3f}",
                    ]
                )
            else:
                row.extend(["", "", "", ""])
        self.log_writer.writerow(row)
        if self.log_handle:
            self.log_handle.flush()

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            if self.last_reading_time and time.monotonic() - self.last_reading_time < 2.0:
                self.status_var.set("Already running")
            else:
                self.status_var.set("Running but no readings; press Stop Power and wait.")
            return
        try:
            power_config = self.power_config_from_fields()
            config = self.meter_config_from_fields()
        except Exception as exc:
            self.status_var.set(str(exc))
            return
        self.app.power_config = power_config
        self.format_fields(power_config)
        save_power_config(self.app.config_path, self.app.power_config)
        self.power_values.clear()
        self.history_values.clear()
        self.last_reading_time = 0.0
        self.power_started_at = time.monotonic()
        self.warmup_seconds = power_config.warmup_seconds
        self.power_var.set("--.- dBFS")
        self.stats_var.set("Avg -- Min -- Max --")
        self.stop_event.clear()
        self.status_var.set(f"Starting... {config.samples_per_update} samples/read")
        self.thread = threading.Thread(target=self.power_loop, args=(config,), daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            meter = self.meter
            if meter:
                meter.cancel()
            self.status_var.set("Stopping...")
        else:
            self.thread = None
            self.meter = None
            self.status_var.set("Stopped")

    def power_loop(self, config: PowerMeterConfig) -> None:
        try:
            with RtlPowerMeter(config) as meter:
                self.meter = meter
                self.app.events.put(("ok", self.refresh_warmup_status, None))
                while not self.stop_event.is_set():
                    reading = meter.read_power()
                    self.app.events.put(("ok", self.update_power, reading))
        except Exception as exc:
            if not self.stop_event.is_set():
                self.app.events.put(("error", self.set_status, str(exc)))
        finally:
            self.meter = None
            self.app.events.put(("ok", self.finish_stopped, None))

    def update_power(self, reading: PowerReading) -> None:
        try:
            smoothing = max(1, int(self.smooth_var.get()))
        except ValueError:
            smoothing = 1
        self.power_values.append(reading.power_dbfs)
        self.last_reading_time = time.monotonic()
        self.power_values = self.power_values[-smoothing:]
        average = sum(self.power_values) / len(self.power_values)
        self.latest_power_dbfs = average
        self.power_var.set(f"{average:0.1f} dBFS")
        self.history_values.append(average)
        self.history_values = self.history_values[-600:]
        history_average = sum(self.history_values) / len(self.history_values)
        self.stats_var.set(
            f"Avg {history_average:0.1f} Min {min(self.history_values):0.1f} Max {max(self.history_values):0.1f}"
        )
        self.log_reading(average)
        self.refresh_warmup_status(None)

    def refresh_warmup_status(self, _unused: object) -> None:
        if self.stop_event.is_set() or not self.power_started_at:
            return
        remaining = self.warmup_seconds - (time.monotonic() - self.power_started_at)
        if remaining > 0:
            self.status_var.set(f"Warming {remaining:0.0f}s")
        else:
            self.status_var.set("Ready")

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def finish_stopped(self, _unused: object) -> None:
        self.thread = None
        if self.stop_event.is_set():
            self.power_values.clear()
            self.history_values.clear()
            self.last_reading_time = 0.0
            self.latest_power_dbfs = None
            self.power_var.set("--.- dBFS")
            self.stats_var.set("Avg -- Min -- Max --")
            self.status_var.set("Stopped")


class ScanCalibrationDialog(tk.Toplevel):
    def __init__(self, app: "WT4App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Scan Calibration")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.status_var = tk.StringVar(value="Track a source and start RTL power before scanning.")
        antenna_names = list(app.configs) or list(app.panels)
        default_antenna = app.scan_config.antenna_name if app.scan_config.antenna_name in antenna_names else ""
        if not default_antenna and antenna_names:
            default_antenna = antenna_names[0]
        self.antenna_var = tk.StringVar(value=default_antenna)
        self.span_var = tk.StringVar(value=f"{app.scan_config.span_degrees:0.1f}")
        self.increment_var = tk.StringVar(value=f"{app.scan_config.increment_degrees:0.2f}")
        self.dwell_var = tk.StringVar(value=f"{app.scan_config.dwell_seconds:0.1f}")
        self.count_var = tk.StringVar(value=str(app.scan_config.scan_count))

        body = ttk.Frame(self, padding=10)
        body.grid(row=0, column=0, sticky="nsew")
        ttk.Label(body, text="Antenna").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Combobox(body, textvariable=self.antenna_var, values=antenna_names, width=12, state="readonly").grid(
            row=0, column=1, sticky="w", pady=2
        )
        self._entry(body, "Span +/- deg", self.span_var, 1)
        self._entry(body, "Increment deg", self.increment_var, 2)
        self._entry(body, "Dwell sec", self.dwell_var, 3)
        self._entry(body, "Scans", self.count_var, 4)
        ttk.Label(body, textvariable=self.status_var, foreground="red", wraplength=360).grid(
            row=5, column=0, columnspan=3, sticky="ew", pady=(8, 0)
        )

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        ttk.Button(buttons, text="AZ Scan", command=lambda: self.start_scan(Axis.AZIMUTH)).pack(side="left")
        ttk.Button(buttons, text="EL Scan", command=lambda: self.start_scan(Axis.ELEVATION)).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Stop Scan", command=app.stop_scan).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Close", command=self.close).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.close)

    def _entry(self, parent: tk.Misc, label: str, variable: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=variable, width=9).grid(row=row, column=1, sticky="w", pady=2)

    def start_scan(self, axis: Axis) -> None:
        try:
            config = ScanConfig(
                span_degrees=float(self.span_var.get()),
                increment_degrees=float(self.increment_var.get()),
                dwell_seconds=float(self.dwell_var.get()),
                scan_count=int(self.count_var.get()),
                antenna_name=self.antenna_var.get().strip(),
            )
            self.app.validate_scan_config(config)
        except ValueError:
            self.status_var.set("Scan parameters must be numeric.")
            return
        except RuntimeError as exc:
            self.status_var.set(str(exc))
            return
        self.span_var.set(f"{config.span_degrees:0.1f}")
        self.increment_var.set(f"{config.increment_degrees:0.2f}")
        self.dwell_var.set(f"{config.dwell_seconds:0.1f}")
        self.count_var.set(str(config.scan_count))
        self.app.start_calibration_scan(axis, config, self)

    def set_status(self, text: str) -> None:
        if self.winfo_exists():
            self.status_var.set(text)

    def close(self) -> None:
        if self.app.scan_dialog is self:
            self.app.scan_dialog = None
        self.destroy()


class ScanGraphDialog(tk.Toplevel):
    def __init__(
        self,
        app: "WT4App",
        axis: Axis,
        rows: list[dict[str, object]],
        csv_path: Path,
        antenna_name: str,
    ) -> None:
        super().__init__(app)
        self.title(f"{antenna_name} {axis_label(axis)} Scan")
        self.resizable(False, False)
        body = ttk.Frame(self, padding=10)
        body.grid(row=0, column=0, sticky="nsew")
        ttk.Label(body, text=f"{antenna_name} {axis_label(axis)} scan saved to {csv_path.name}").grid(
            row=0, column=0, sticky="w"
        )
        self.summary_var = tk.StringVar(value="Fit --")
        canvas = tk.Canvas(body, width=520, height=300, background="white")
        canvas.grid(row=1, column=0, pady=(8, 0))
        self.draw_plot(canvas, axis, rows)
        ttk.Label(body, textvariable=self.summary_var).grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Button(body, text="Close", command=self.destroy).grid(row=3, column=0, sticky="e", pady=(8, 0))

    def draw_plot(self, canvas: tk.Canvas, axis: Axis, rows: list[dict[str, object]]) -> None:
        width = int(canvas["width"])
        height = int(canvas["height"])
        left, right, top, bottom = 55, width - 20, 20, height - 45
        scan_points = [
            (float(row["offset_degrees"]), float(row["power_dbfs"]))
            for row in rows
            if row.get("power_dbfs") is not None
        ]
        if not scan_points:
            canvas.create_text(width / 2, height / 2, text="No scan data")
            return
        offsets = [point[0] for point in scan_points]
        powers = [point[1] for point in scan_points]
        fit = self.fit_gaussian_with_slope(scan_points)
        fit_points: list[tuple[float, float]] = []
        if fit:
            min_fit_x, max_fit_x = min(offsets), max(offsets)
            for index in range(101):
                x_value = min_fit_x + (max_fit_x - min_fit_x) * index / 100.0
                fit_points.append((x_value, self.evaluate_fit(fit, x_value)))
            powers.extend(y for _x, y in fit_points)
        min_x, max_x = min(offsets), max(offsets)
        min_y, max_y = min(powers), max(powers)
        if min_x == max_x:
            min_x -= 1.0
            max_x += 1.0
        if min_y == max_y:
            min_y -= 0.5
            max_y += 0.5
        self.draw_graticule(canvas, left, right, top, bottom, min_x, max_x, min_y, max_y)
        canvas.create_line(left, bottom, right, bottom)
        canvas.create_line(left, top, left, bottom)
        canvas.create_text((left + right) / 2, height - 15, text=f"{axis_label(axis)} offset degrees")
        canvas.create_text(18, (top + bottom) / 2, text="dBFS", angle=90)
        self.draw_boresight(canvas, left, right, top, bottom, min_x, max_x)

        if fit_points:
            canvas_fit_points = [
                self.canvas_point(x_value, y_value, left, right, top, bottom, min_x, max_x, min_y, max_y)
                for x_value, y_value in fit_points
            ]
            for start, end in zip(canvas_fit_points, canvas_fit_points[1:]):
                canvas.create_line(start[0], start[1], end[0], end[1], fill="#d62728", width=2)
            fwhm = 2.35482 * fit["sigma"]
            self.summary_var.set(
                f"Fit centre {fit['center']:+0.3f} deg, FWHM {fwhm:0.3f} deg, "
                f"peak {fit['peak']:0.2f} dBFS, RMS {fit['rms']:0.3f} dB"
            )
        else:
            self.summary_var.set("Fit unavailable")

        points = [
            self.canvas_point(x_value, y_value, left, right, top, bottom, min_x, max_x, min_y, max_y)
            for x_value, y_value in scan_points
        ]
        for start, end in zip(points, points[1:]):
            canvas.create_line(start[0], start[1], end[0], end[1], fill="#0057b8", width=2)
        for x, y in points:
            canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#0057b8", outline="")

    def canvas_point(
        self,
        x_value: float,
        y_value: float,
        left: int,
        right: int,
        top: int,
        bottom: int,
        min_x: float,
        max_x: float,
        min_y: float,
        max_y: float,
    ) -> tuple[float, float]:
        x = left + (x_value - min_x) / (max_x - min_x) * (right - left)
        y = bottom - (y_value - min_y) / (max_y - min_y) * (bottom - top)
        return x, y

    def draw_boresight(
        self,
        canvas: tk.Canvas,
        left: int,
        right: int,
        top: int,
        bottom: int,
        min_x: float,
        max_x: float,
    ) -> None:
        if not (min_x <= 0.0 <= max_x):
            return
        x, _y = self.canvas_point(0.0, 0.0, left, right, top, bottom, min_x, max_x, 0.0, 1.0)
        canvas.create_line(x, top, x, bottom, fill="#444444", dash=(4, 3), width=2)
        canvas.create_text(x + 4, top + 10, text="boresight", anchor="w", fill="#444444")

    def fit_gaussian_with_slope(self, points: list[tuple[float, float]]) -> Optional[dict[str, float]]:
        if len(points) < 5:
            return None
        points = sorted(points)
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        min_x, max_x = min(xs), max(xs)
        span = max_x - min_x
        if span <= 0.0:
            return None
        peak_x = xs[ys.index(max(ys))]
        sigma_min = max(span / 30.0, 0.02)
        sigma_max = max(span, sigma_min * 2.0)
        center_start = max(min_x, peak_x - span * 0.25)
        center_stop = min(max_x, peak_x + span * 0.25)
        best: Optional[dict[str, float]] = None
        for center in self.fit_range(center_start, center_stop, 41):
            for sigma in self.fit_range(sigma_min, sigma_max, 50):
                fit = self.solve_linear_fit(points, center, sigma)
                if fit and fit["amplitude"] > 0.0 and (best is None or fit["sse"] < best["sse"]):
                    best = fit
        if not best:
            return None
        for center_width, sigma_factor in ((span * 0.08, 0.35), (span * 0.03, 0.18)):
            center_start = max(min_x, best["center"] - center_width)
            center_stop = min(max_x, best["center"] + center_width)
            sigma_start = max(sigma_min, best["sigma"] * (1.0 - sigma_factor))
            sigma_stop = min(sigma_max, best["sigma"] * (1.0 + sigma_factor))
            for center in self.fit_range(center_start, center_stop, 41):
                for sigma in self.fit_range(sigma_start, sigma_stop, 41):
                    fit = self.solve_linear_fit(points, center, sigma)
                    if fit and fit["amplitude"] > 0.0 and fit["sse"] < best["sse"]:
                        best = fit
        best["rms"] = math.sqrt(best["sse"] / len(points))
        best["peak"] = self.evaluate_fit(best, best["center"])
        return best

    def fit_range(self, start: float, stop: float, count: int) -> list[float]:
        if count <= 1 or start == stop:
            return [start]
        return [start + (stop - start) * index / (count - 1) for index in range(count)]

    def solve_linear_fit(
        self,
        points: list[tuple[float, float]],
        center: float,
        sigma: float,
    ) -> Optional[dict[str, float]]:
        rows = []
        for x_value, y_value in points:
            gaussian = math.exp(-0.5 * ((x_value - center) / sigma) ** 2)
            rows.append((1.0, x_value, gaussian, y_value))
        normal = [[0.0 for _ in range(3)] for _ in range(3)]
        rhs = [0.0, 0.0, 0.0]
        for row in rows:
            values = row[:3]
            y_value = row[3]
            for i in range(3):
                rhs[i] += values[i] * y_value
                for j in range(3):
                    normal[i][j] += values[i] * values[j]
        solution = self.solve_3x3(normal, rhs)
        if solution is None:
            return None
        baseline, slope, amplitude = solution
        sse = 0.0
        for x_value, y_value in points:
            predicted = baseline + slope * x_value + amplitude * math.exp(-0.5 * ((x_value - center) / sigma) ** 2)
            sse += (y_value - predicted) ** 2
        return {
            "baseline": baseline,
            "slope": slope,
            "amplitude": amplitude,
            "center": center,
            "sigma": sigma,
            "sse": sse,
        }

    def solve_3x3(self, matrix: list[list[float]], rhs: list[float]) -> Optional[list[float]]:
        augmented = [matrix[row][:] + [rhs[row]] for row in range(3)]
        for column in range(3):
            pivot = max(range(column, 3), key=lambda row: abs(augmented[row][column]))
            if abs(augmented[pivot][column]) < 1e-12:
                return None
            augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
            pivot_value = augmented[column][column]
            for item in range(column, 4):
                augmented[column][item] /= pivot_value
            for row in range(3):
                if row == column:
                    continue
                factor = augmented[row][column]
                for item in range(column, 4):
                    augmented[row][item] -= factor * augmented[column][item]
        return [augmented[row][3] for row in range(3)]

    def evaluate_fit(self, fit: dict[str, float], x_value: float) -> float:
        gaussian = math.exp(-0.5 * ((x_value - fit["center"]) / fit["sigma"]) ** 2)
        return fit["baseline"] + fit["slope"] * x_value + fit["amplitude"] * gaussian

    def draw_graticule(
        self,
        canvas: tk.Canvas,
        left: int,
        right: int,
        top: int,
        bottom: int,
        min_x: float,
        max_x: float,
        min_y: float,
        max_y: float,
    ) -> None:
        divisions = 5
        grid_color = "#d9d9d9"
        for index in range(divisions + 1):
            fraction = index / divisions
            x = left + fraction * (right - left)
            x_value = min_x + fraction * (max_x - min_x)
            canvas.create_line(x, top, x, bottom, fill=grid_color)
            canvas.create_text(x, bottom + 14, text=f"{x_value:0.1f}", anchor="n")

            y = bottom - fraction * (bottom - top)
            y_value = min_y + fraction * (max_y - min_y)
            canvas.create_line(left, y, right, y, fill=grid_color)
            canvas.create_text(left - 8, y, text=f"{y_value:0.1f}", anchor="e")


class WT4App(tk.Tk):
    def __init__(self, config_path: str) -> None:
        super().__init__()
        self.title(f"WT4 Antenna Controller {APP_VERSION}")
        self.geometry("900x620")
        self.minsize(860, 580)
        self.config_path = config_path
        self.configs = load_configs(config_path)
        self.site = load_site_config(config_path)
        self.sources = load_sources(config_path)
        self.power_config = load_power_config(config_path)
        self.scan_config = load_scan_config(config_path)
        self.sessions: dict[str, SafeAntenna] = {}
        self.events: queue.Queue[tuple[str, object, object]] = queue.Queue()
        self.tracking_stop_event = threading.Event()
        self.park_stop_event = threading.Event()
        self.scan_stop_event = threading.Event()
        self.motion_lock = threading.Lock()
        self.scan_offset_lock = threading.Lock()
        self.tracking_active = False
        self.tracking_thread: Optional[threading.Thread] = None
        self.scan_thread: Optional[threading.Thread] = None
        self.tracking_last_update = 0.0
        self.parking_active = False
        self.scan_active = False
        self.scan_antenna_name = ""
        self.scan_axis: Optional[Axis] = None
        self.scan_offset_degrees = 0.0
        self.tracking_kind = ""
        self.current_target: Optional[TargetPosition] = None
        self.target_name_var = tk.StringVar(value="Target --")
        self.target_az_var = tk.StringVar(value="AZ --")
        self.target_el_var = tk.StringVar(value="EL --")
        self.target_ha_var = tk.StringVar(value="HA --")
        self.sun_ref_var = tk.StringVar(value="Sun AZ -- EL --")
        self.moon_ref_var = tk.StringVar(value="Moon AZ -- EL --")
        self.local_time_var = tk.StringVar(value="Local --")
        self.lmst_var = tk.StringVar(value="LMST --")
        self.utc_var = tk.StringVar(value="UTC --")
        self.calibration_dialog: Optional[CalibrationDialog] = None
        self.peak_calibration_dialog: Optional[PeakCalibrationDialog] = None
        self.scan_dialog: Optional[ScanCalibrationDialog] = None

        self.status_var = tk.StringVar(value="Load config, connect antennas, then use guarded jogs.")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        top = ttk.Frame(self, padding=(8, 8, 8, 2))
        top.pack(fill="x")
        top_row_1 = ttk.Frame(top)
        top_row_1.pack(fill="x")
        top_row_2 = ttk.Frame(top)
        top_row_2.pack(fill="x", pady=(4, 0))
        ttk.Button(top_row_1, text="Connect", command=self.connect_all).pack(side="left")
        ttk.Button(top_row_1, text="Disconnect", command=self.disconnect_all).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="Limits", command=self.open_limits).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="Observer", command=self.open_observer).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="Tracking", command=self.open_tracking).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="Sources", command=self.open_sources).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="Calibration", command=self.open_calibration).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="Peak Cal", command=self.open_peak_calibration).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="Scan Cal", command=self.open_scan_calibration).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="Encoders", command=self.open_encoders).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="STOP ALL", command=self.stop_all).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_2, text="Track Sun", command=lambda: self.start_tracking("sun")).pack(side="left")
        ttk.Button(top_row_2, text="Track Moon", command=lambda: self.start_tracking("moon")).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_2, text="Track Source", command=lambda: self.start_tracking("source")).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_2, text="Stop Track", command=self.stop_sun_tracking).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_2, text="Park", command=self.park_all).pack(side="left", padx=(6, 0))

        target_bar = ttk.Frame(self, padding=(8, 0, 8, 2))
        target_bar.pack(fill="x")
        ttk.Label(target_bar, textvariable=self.target_name_var).pack(side="left")
        ttk.Label(target_bar, textvariable=self.target_az_var).pack(side="left", padx=(16, 0))
        ttk.Label(target_bar, textvariable=self.target_el_var).pack(side="left", padx=(16, 0))
        target_detail_bar = ttk.Frame(self, padding=(8, 0, 8, 2))
        target_detail_bar.pack(fill="x")
        ttk.Label(target_detail_bar, textvariable=self.target_ha_var).pack(side="left")

        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        self.panels: dict[str, AntennaPanel] = {}
        names = list(self.configs) or ["antenna_a", "antenna_b"]
        for index, name in enumerate(names[:2]):
            panel = AntennaPanel(body, self, name, self.configs.get(name))
            panel.grid(row=0, column=index, sticky="nsew", padx=4)
            if index == 0:
                panel.add_reference_block(
                    self.sun_ref_var,
                    self.moon_ref_var,
                    self.local_time_var,
                    self.lmst_var,
                    self.utc_var,
                )
            self.panels[name] = panel

        self.power_panel = PowerMeterPanel(body, self)
        self.power_panel.grid(row=1, column=0, columnspan=2, sticky="ew", padx=4, pady=(10, 0))

        if not self.configs:
            self.status_var.set(f"No antennas found in {config_path}. Copy wt4.ini.example to wt4.ini.")

        self.after(100, self.process_events)
        self.update_reference_positions()
        self.after(1500, self.periodic_refresh)

    def connect_all(self) -> None:
        for name, config in self.configs.items():
            if name in self.sessions:
                continue
            self.run_worker(
                lambda cfg=config: self.connect_session(cfg),
                lambda session, n=name: self.attach_session(n, session),
                self.set_status,
            )

    def connect_session(self, config) -> SafeAntenna:
        session = SafeAntenna(config)
        session.update_oled("MANUAL", activity="STOPPED")
        return session

    def attach_session(self, name: str, session: SafeAntenna) -> None:
        self.sessions[name] = session
        if name in self.panels:
            self.panels[name].attach(session)
        connected = len(self.sessions)
        total = len(self.configs)
        self.status_var.set(f"Stopped. Connected {connected}/{total} antennas.")

    def disconnect_all(self) -> None:
        if self.parking_active:
            self.status_var.set("Parking in progress.")
            return
        if not self.sessions:
            self.status_var.set("Already disconnected.")
            return
        for panel in self.panels.values():
            panel.stop_event.set()
        for name, session in list(self.sessions.items()):
            self.run_worker(
                lambda s=session: s.close(),
                lambda _result, n=name: self.detach_session(n),
                self.set_status,
            )
        self.status_var.set("Disconnecting...")

    def detach_session(self, name: str) -> None:
        self.sessions.pop(name, None)
        if name in self.panels:
            self.panels[name].detach()
        connected = len(self.sessions)
        total = len(self.configs)
        if connected:
            self.status_var.set(f"Stopped. Connected {connected}/{total} antennas.")
        else:
            self.status_var.set("Disconnected.")

    def refresh_all(self) -> None:
        for panel in self.panels.values():
            panel.refresh()

    def oled_all(self) -> None:
        for session in self.sessions.values():
            self.run_worker(
                lambda s=session: s.update_oled("MANUAL", activity="STOPPED"),
                lambda _result: None,
                self.set_status,
            )

    def start_tracking(self, kind: str) -> None:
        if self.tracking_active:
            self.status_var.set("Tracking already active.")
            return
        if not self.sessions:
            self.status_var.set("Connect antennas before tracking.")
            return
        try:
            self.validate_site(self.site)
            self.target_for_kind(kind)
        except RuntimeError as exc:
            self.status_var.set(str(exc))
            return
        self.tracking_stop_event.clear()
        self.tracking_active = True
        self.tracking_kind = kind
        self.tracking_last_update = time.monotonic()
        self.status_var.set(f"Slewing to {self.kind_label(kind)}.")
        self.tracking_thread = threading.Thread(target=lambda: self.tracking_loop(kind), daemon=True)
        self.tracking_thread.start()

    def stop_sun_tracking(self) -> None:
        self.tracking_stop_event.set()
        self.tracking_active = False
        self.tracking_kind = ""
        self.stop_scan()
        self.target_ha_var.set("HA --")
        self.stop_all()
        self.status_var.set("Stopped.")

    def validate_scan_config(self, config: ScanConfig) -> None:
        if config.antenna_name not in self.configs:
            raise RuntimeError("Select East or West antenna for the scan.")
        if config.antenna_name not in self.sessions:
            raise RuntimeError(f"{config.antenna_name} must be connected before scanning.")
        if not (0.1 <= config.span_degrees <= 30.0):
            raise RuntimeError("Scan span must be 0.1..30.0 degrees.")
        if not (0.01 <= config.increment_degrees <= config.span_degrees):
            raise RuntimeError("Scan increment must be 0.01 degrees up to the scan span.")
        if not (0.1 <= config.dwell_seconds <= 60.0):
            raise RuntimeError("Dwell must be 0.1..60.0 seconds.")
        if not (1 <= config.scan_count <= 20):
            raise RuntimeError("Scan count must be 1..20.")

    def start_calibration_scan(self, axis: Axis, config: ScanConfig, dialog: ScanCalibrationDialog) -> None:
        if self.scan_thread and self.scan_thread.is_alive():
            dialog.set_status("Scan already running.")
            return
        if not self.tracking_active or not self.tracking_kind:
            dialog.set_status("Start tracking Sun, Moon, or Source before scanning.")
            return
        if not self.sessions:
            dialog.set_status("Connect antennas before scanning.")
            return
        if self.power_panel.latest_power_dbfs is None:
            dialog.set_status("Start RTL power and wait for readings before scanning.")
            return
        try:
            self.validate_scan_config(config)
        except RuntimeError as exc:
            dialog.set_status(str(exc))
            return
        self.scan_config = config
        save_scan_config(self.config_path, config)
        self.scan_stop_event.clear()
        self.scan_active = True
        dialog.set_status(f"{axis_label(axis)} scan starting on {config.antenna_name}...")
        self.status_var.set(f"{axis_label(axis)} scan starting on {config.antenna_name}.")
        self.scan_thread = threading.Thread(target=lambda: self.scan_worker(axis, config, dialog), daemon=True)
        self.scan_thread.start()

    def stop_scan(self) -> None:
        self.scan_stop_event.set()
        self.scan_active = False
        self.set_scan_offset(None)
        if self.scan_dialog and self.scan_dialog.winfo_exists():
            self.scan_dialog.set_status("Scan stopping; returning to nominal target.")

    def scan_offsets(self, config: ScanConfig) -> list[float]:
        offsets: list[float] = []
        value = -config.span_degrees
        limit = config.span_degrees + config.increment_degrees * 0.5
        while value <= limit:
            offsets.append(round(value, 6))
            value += config.increment_degrees
        if offsets and offsets[-1] > config.span_degrees:
            offsets[-1] = config.span_degrees
        return offsets

    def scan_worker(self, axis: Axis, config: ScanConfig, dialog: ScanCalibrationDialog) -> None:
        rows: list[dict[str, object]] = []
        averaged_rows: list[dict[str, object]] = []
        csv_path = Path(f"wt4_scan_{config.antenna_name.lower()}_{axis_label(axis).lower()}_{datetime.now():%Y%m%d-%H%M%S}.csv")
        try:
            offsets = self.scan_offsets(config)
            total_points = len(offsets) * config.scan_count
            point_index = 0
            for scan_number in range(1, config.scan_count + 1):
                if self.scan_stop_event.is_set():
                    break
                self.move_scan_to_start(axis, config, offsets[0])
                for offset in offsets:
                    if self.scan_stop_event.is_set():
                        break
                    point_index += 1
                    nominal = self.current_tracking_target(self.tracking_kind)
                    self.set_scan_offset(config.antenna_name, axis, offset)
                    target = self.apply_scan_offset(nominal, config.antenna_name)
                    self.events.put(
                        (
                            "ok",
                            dialog.set_status,
                            f"{config.antenna_name} {axis_label(axis)} scan {scan_number}/{config.scan_count} "
                            f"point {point_index}/{total_points} offset {offset:+0.2f} deg",
                        )
                    )
                    self.events.put(("ok", self.set_status, f"{config.antenna_name} {axis_label(axis)} scan offset {offset:+0.2f} deg."))
                    self.slew_all_to_target(nominal, "SCAN", show_slewing=False)
                    row = self.collect_scan_point(axis, offset, config.dwell_seconds, nominal, target, config.antenna_name, scan_number)
                    rows.append(row)
            averaged_rows = self.average_scan_rows(rows, offsets)
            self.write_scan_csv(csv_path, rows, averaged_rows)
            self.set_scan_offset(None)
            if self.tracking_kind and not self.tracking_stop_event.is_set():
                self.slew_all_to_target(self.current_tracking_target(self.tracking_kind), "SCAN", show_slewing=False)
            if averaged_rows:
                self.events.put(("ok", lambda _unused: ScanGraphDialog(self, axis, averaged_rows, csv_path, config.antenna_name), None))
                self.events.put(("ok", dialog.set_status, f"Scan complete: {csv_path.name}"))
                self.events.put(("ok", self.set_status, f"Scan complete: {csv_path.name}"))
            else:
                self.events.put(("ok", dialog.set_status, "Scan stopped before measurements were taken."))
        except Exception as exc:
            self.scan_stop_event.set()
            self.set_scan_offset(None)
            self.events.put(("error", dialog.set_status, str(exc)))
            self.events.put(("error", self.set_status, f"Scan fault: {exc}"))
        finally:
            self.scan_active = False

    def collect_scan_point(
        self,
        axis: Axis,
        offset: float,
        dwell_seconds: float,
        nominal: TargetPosition,
        target: TargetPosition,
        antenna_name: str,
        scan_number: int,
    ) -> dict[str, object]:
        powers: list[float] = []
        end_time = time.monotonic() + dwell_seconds
        while not self.scan_stop_event.is_set() and time.monotonic() < end_time:
            power = self.power_panel.latest_power_dbfs
            if power is not None:
                powers.append(power)
            time.sleep(0.1)
        now_local = datetime.now().astimezone()
        row: dict[str, object] = {
            "local_time": now_local.isoformat(timespec="milliseconds"),
            "antenna": antenna_name,
            "axis": axis_label(axis),
            "scan_number": scan_number,
            "offset_degrees": offset,
            "nominal_az": nominal.azimuth,
            "nominal_el": nominal.elevation,
            "target_az": target.azimuth,
            "target_el": target.elevation,
            "power_dbfs": sum(powers) / len(powers) if powers else None,
            "sample_count": len(powers),
        }
        for name, panel in self.panels.items():
            position = panel.session.last_position if panel.session else None
            row[f"{name}_az"] = position.azimuth if position else None
            row[f"{name}_el"] = position.elevation if position else None
            row[f"{name}_raw_az"] = position.raw_azimuth if position else None
            row[f"{name}_raw_el"] = position.raw_elevation if position else None
        return row

    def average_scan_rows(self, rows: list[dict[str, object]], offsets: list[float]) -> list[dict[str, object]]:
        averaged: list[dict[str, object]] = []
        for offset in offsets:
            matching = [row for row in rows if row.get("power_dbfs") is not None and float(row["offset_degrees"]) == offset]
            if not matching:
                continue
            power = sum(float(row["power_dbfs"]) for row in matching) / len(matching)
            template = dict(matching[-1])
            template["power_dbfs"] = power
            template["sample_count"] = sum(int(row.get("sample_count", 0)) for row in matching)
            template["scan_number"] = "avg"
            averaged.append(template)
        return averaged

    def move_scan_to_start(self, axis: Axis, config: ScanConfig, start_offset: float) -> None:
        self.set_scan_offset(config.antenna_name, axis, start_offset)
        if self.tracking_kind and not self.scan_stop_event.is_set():
            self.slew_all_to_target(self.current_tracking_target(self.tracking_kind), "SCAN", show_slewing=False)

    def write_scan_csv(self, csv_path: Path, rows: list[dict[str, object]], averaged_rows: list[dict[str, object]]) -> None:
        if not rows:
            return
        fieldnames = list(rows[0])
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
            if averaged_rows:
                writer.writerow({field: "" for field in fieldnames})
                for row in averaged_rows:
                    writer.writerow(row)

    def prepare_peak_calibration_owner(self) -> None:
        if self.parking_active:
            raise RuntimeError("Stop parking before using Peak Calibration.")
        thread = self.tracking_thread
        if not self.tracking_active and not (thread and thread.is_alive()):
            return

        self.tracking_stop_event.set()
        if thread and thread.is_alive() and threading.current_thread() is not thread:
            max_jog = max((session.config.limits.max_jog_seconds for session in self.sessions.values()), default=60.0)
            timeout = min(10.0, max(2.0, self.site.track_interval_seconds + max_jog * 0.1))
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise RuntimeError("Main tracking is still stopping; try Peak Calibration again in a moment.")
        self.tracking_active = False
        self.tracking_kind = None
        self.status_var.set("Tracking stopped for Peak Calibration.")

    def park_all(self) -> None:
        if self.parking_active:
            self.status_var.set("Parking already in progress.")
            return
        if not self.sessions:
            self.status_var.set("Connect antennas before parking.")
            return
        try:
            for name, session in self.sessions.items():
                session.config.limits.assert_position_allowed(session.config.park_az, session.config.park_el)
        except Exception as exc:
            self.status_var.set(f"Park position invalid: {exc}")
            return

        self.tracking_stop_event.set()
        self.tracking_active = False
        self.park_stop_event.clear()
        self.parking_active = True
        self.status_var.set("Parking antennas...")
        threading.Thread(target=self.park_worker, daemon=True).start()

    def park_worker(self) -> None:
        sessions = list(self.sessions.items())
        errors: list[str] = []
        lock = threading.Lock()

        def make_worker(name: str, session: SafeAntenna):
            panel = self.panels.get(name)

            def progress(position: Position) -> None:
                if panel:
                    self.events.put(("position", panel.update_position, position))
                session.update_oled_position(session.config.park_az, session.config.park_el, "PARKING")

            def worker() -> None:
                try:
                    if panel:
                        self.events.put(("ok", panel.set_tracking_status, "PARKING"))
                    session.update_oled("PARK", session.config.park_az, session.config.park_el, "PARKING")
                    position = session.guarded_slew_to(
                        session.config.park_az,
                        session.config.park_el,
                        session.config.az_track_speed,
                        session.config.el_track_speed,
                        self.park_stop_event,
                        self.az_tracking_start_tolerance(),
                        self.el_tracking_start_tolerance(),
                        self.az_tracking_stop_tolerance(),
                        self.el_tracking_stop_tolerance(),
                        self.site.az_slow_speed,
                        self.site.el_slow_speed,
                        self.site.az_slow_threshold_degrees,
                        self.site.el_slow_threshold_degrees,
                        progress,
                    )
                    if self.park_stop_event.is_set():
                        raise RuntimeError("Park cancelled.")
                    session.update_oled("PARK", session.config.park_az, session.config.park_el, "PARKED")
                    if panel:
                        self.events.put(("position", panel.update_position, position))
                        self.events.put(("ok", panel.set_tracking_status, "PARKED"))
                except Exception as exc:
                    if panel:
                        self.events.put(("error", panel.set_fault, str(exc)))
                    with lock:
                        errors.append(f"{name}: {exc}")

            return worker

        threads = [threading.Thread(target=make_worker(name, session), daemon=True) for name, session in sessions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        if errors:
            for _name, session in sessions:
                try:
                    session.stop_all()
                except Exception:
                    pass
            self.events.put(("error", self.finish_park_fault, "; ".join(errors)))
            return

        closed_names: list[str] = []
        for name, session in sessions:
            try:
                session.close()
                closed_names.append(name)
            except Exception as exc:
                errors.append(f"{name}: disconnect failed after park: {exc}")
        if errors:
            self.events.put(("error", self.finish_park_fault, "; ".join(errors)))
            return
        self.events.put(("ok", self.finish_park_success, closed_names))

    def finish_park_success(self, names: list[str]) -> None:
        for name in names:
            self.detach_session(name)
        self.parking_active = False
        self.park_stop_event.clear()
        self.status_var.set("Parked and disconnected.")

    def finish_park_fault(self, message: str) -> None:
        self.parking_active = False
        self.park_stop_event.clear()
        for panel in self.panels.values():
            if panel.session and panel.status_var.get() == "PARKING":
                panel.status_var.set("STOPPED")
        self.status_var.set(f"Park fault: {message}")

    def tracking_loop(self, kind: str) -> None:
        acquired = False
        try:
            while not self.tracking_stop_event.is_set():
                target = self.current_tracking_target(kind)
                self.tracking_last_update = time.monotonic()
                self.events.put(("ok", self.apply_target_position, target))
                if not acquired:
                    self.events.put(("ok", self.set_status, f"Slewing to {target.name}."))
                self.slew_all_to_target(target, target.name[:8].upper(), show_slewing=not acquired)
                if self.tracking_stop_event.is_set():
                    break
                acquired = True
                self.tracking_last_update = time.monotonic()
                self.events.put(("ok", self.finish_target_slew, target))
                wait_until = time.monotonic() + max(0.1, self.site.track_interval_seconds)
                while not self.tracking_stop_event.is_set() and time.monotonic() < wait_until:
                    time.sleep(0.05)
        except Exception as exc:
            self.tracking_stop_event.set()
            self.events.put(("error", self.finish_tracking_fault, str(exc)))
        finally:
            self.tracking_active = False

    def target_for_kind(self, kind: str, when: Optional[datetime] = None) -> TargetPosition:
        if kind == "sun":
            sun = sun_position(self.site.latitude, self.site.longitude, when)
            return TargetPosition("Sun", sun.azimuth, sun.elevation)
        if kind == "moon":
            return moon_position(self.site.latitude, self.site.longitude, when)
        if kind == "source":
            source = self.selected_source()
            return source_position(
                source.name,
                source.ra_hours,
                source.dec_degrees,
                self.site.latitude,
                self.site.longitude,
                when,
            )
        raise RuntimeError(f"Unknown target type: {kind}")

    def selected_source(self) -> SourceConfig:
        if not self.site.selected_source:
            raise RuntimeError("Open Sources and select a source before source tracking.")
        source = self.sources.get(self.site.selected_source)
        if source is None:
            raise RuntimeError(f"Selected source {self.site.selected_source!r} was not found.")
        return source

    def current_tracking_target(self, kind: str) -> TargetPosition:
        source = self.target_for_kind(kind)
        az_tolerance = self.site.az_track_tolerance_degrees
        el_tolerance = self.site.el_track_tolerance_degrees
        if az_tolerance >= 0 and el_tolerance >= 0:
            return source

        now = datetime.now(timezone.utc)
        future = self.target_for_kind(kind, now + timedelta(seconds=60))
        az_delta = shortest_angle_delta(source.azimuth, future.azimuth)
        el_delta = future.elevation - source.elevation
        azimuth = source.azimuth
        elevation = source.elevation
        if az_tolerance < 0 and az_delta != 0.0:
            azimuth = (azimuth + abs(az_tolerance) * (1.0 if az_delta > 0 else -1.0)) % 360.0
        if el_tolerance < 0 and el_delta != 0.0:
            elevation += abs(el_tolerance) * (1.0 if el_delta > 0 else -1.0)
        target = TargetPosition(
            name=source.name,
            azimuth=azimuth,
            elevation=elevation,
        )
        return target

    def apply_scan_offset(self, target: TargetPosition, antenna_name: str) -> TargetPosition:
        with self.scan_offset_lock:
            scan_antenna_name = self.scan_antenna_name
            axis = self.scan_axis
            offset = self.scan_offset_degrees
        if antenna_name != scan_antenna_name or axis is None or offset == 0.0:
            return target
        if axis == Axis.AZIMUTH:
            return TargetPosition(target.name, (target.azimuth + offset) % 360.0, target.elevation)
        return TargetPosition(target.name, target.azimuth, max(0.0, min(90.0, target.elevation + offset)))

    def set_scan_offset(self, antenna_name: Optional[str], axis: Optional[Axis] = None, offset: float = 0.0) -> None:
        with self.scan_offset_lock:
            self.scan_antenna_name = antenna_name or ""
            self.scan_axis = axis
            self.scan_offset_degrees = offset if antenna_name and axis else 0.0

    def validate_site(self, site: SiteConfig) -> None:
        self.validate_observer(site)
        if not (0.1 <= site.track_interval_seconds <= 10.0):
            raise RuntimeError("Tracking interval must be 0.1..10.0 seconds.")
        self._validate_axis_tracking(
            "AZ",
            site.az_track_tolerance_degrees,
            site.az_stop_tolerance_degrees,
            site.az_slow_speed,
            site.az_slow_threshold_degrees,
        )
        self._validate_axis_tracking(
            "EL",
            site.el_track_tolerance_degrees,
            site.el_stop_tolerance_degrees,
            site.el_slow_speed,
            site.el_slow_threshold_degrees,
        )

    def _validate_axis_tracking(
        self,
        axis: str,
        start_tolerance: float,
        stop_tolerance: float,
        slow_speed: int,
        slow_threshold: float,
    ) -> None:
        if not (-0.2 <= start_tolerance <= 0.2) or start_tolerance == 0.0:
            raise RuntimeError(f"{axis} start tolerance must be -0.20..-0.01 or 0.01..0.20 degrees.")
        if not (-abs(start_tolerance) <= stop_tolerance <= abs(start_tolerance)) or stop_tolerance == 0.0:
            raise RuntimeError(f"{axis} stop tolerance must be +/-0.01 degrees up to the start tolerance.")
        if not (1 <= slow_speed <= 100):
            raise RuntimeError(f"{axis} slow speed must be 1..100.")
        if not (abs(start_tolerance) <= slow_threshold <= 30.0):
            raise RuntimeError(f"{axis} slow deg must be at least start tolerance and no more than 30 degrees.")

    def validate_observer(self, site: SiteConfig) -> None:
        if not (-90.0 <= site.latitude <= 90.0):
            raise RuntimeError("Latitude must be -90..90 degrees.")
        if not (-180.0 <= site.longitude <= 180.0):
            raise RuntimeError("Longitude must be -180..180 degrees.")

    def apply_target_position(self, target: TargetPosition) -> None:
        self.current_target = target
        self.target_name_var.set(target.name)
        self.target_az_var.set(f"AZ {target.azimuth:0.2f}")
        self.target_el_var.set(f"EL {target.elevation:0.2f}")
        self.target_ha_var.set(self.current_hour_angle_text())

    def current_hour_angle_text(self) -> str:
        now = datetime.now(timezone.utc)
        try:
            if self.tracking_kind == "sun":
                ra_hours = sun_equatorial(now).ra_hours
            elif self.tracking_kind == "moon":
                ra_hours = moon_equatorial(now)[0].ra_hours
            elif self.tracking_kind == "source":
                ra_hours = self.selected_source().ra_hours
            else:
                return "HA --"
        except RuntimeError:
            return "HA --"
        hour_angle_degrees = local_sidereal_time(self.site.longitude, now) - ra_hours * 15.0
        hour_angle_degrees = self.wrap_signed_degrees(hour_angle_degrees)
        return f"HA {self.format_hour_angle(hour_angle_degrees)}"

    def wrap_signed_degrees(self, value: float) -> float:
        while value <= -180.0:
            value += 360.0
        while value > 180.0:
            value -= 360.0
        return value

    def format_hour_angle(self, hour_angle_degrees: float) -> str:
        sign = "+" if hour_angle_degrees >= 0.0 else "-"
        total_minutes = int(round(abs(hour_angle_degrees) / 15.0 * 60.0))
        hours, minutes = divmod(total_minutes, 60)
        return f"{sign}{hours:02d}:{minutes:02d}"

    def slew_all_to_target(self, target: TargetPosition, mode: str, show_slewing: bool = True) -> TargetPosition:
        with self.motion_lock:
            return self._slew_all_to_target(target, mode, show_slewing)

    def _slew_all_to_target(self, target: TargetPosition, mode: str, show_slewing: bool = True) -> TargetPosition:
        errors: list[str] = []
        threads: list[threading.Thread] = []
        lock = threading.Lock()

        def make_worker(name: str, session: SafeAntenna, panel: AntennaPanel):
            activity = "SLEWING" if show_slewing else "TRACKING"
            effective_target = self.apply_scan_offset(target, name)

            def progress(position: Position) -> None:
                self.events.put(("position", panel.update_position, position))
                session.update_oled_position(effective_target.azimuth, effective_target.elevation, activity)

            def worker() -> None:
                try:
                    self.events.put(("ok", panel.set_tracking_status, activity))
                    session.update_oled(mode, effective_target.azimuth, effective_target.elevation, activity)
                    position = session.guarded_slew_to(
                        effective_target.azimuth,
                        effective_target.elevation,
                        session.config.az_track_speed,
                        session.config.el_track_speed,
                        self.tracking_stop_event,
                        self.az_tracking_start_tolerance(),
                        self.el_tracking_start_tolerance(),
                        self.az_tracking_stop_tolerance(),
                        self.el_tracking_stop_tolerance(),
                        self.site.az_slow_speed,
                        self.site.el_slow_speed,
                        self.site.az_slow_threshold_degrees,
                        self.site.el_slow_threshold_degrees,
                        progress,
                    )
                    if self.tracking_stop_event.is_set():
                        session.update_oled(mode, effective_target.azimuth, effective_target.elevation, "STOPPED")
                        self.events.put(("position", panel.update_position, position))
                        self.events.put(("ok", panel.set_tracking_status, "STOPPED"))
                        return
                    session.update_oled(mode, effective_target.azimuth, effective_target.elevation, "TRACKING")
                    self.events.put(("position", panel.update_position, position))
                    self.events.put(("ok", panel.set_tracking_status, "TRACKING"))
                except Exception as exc:
                    self.events.put(("error", panel.set_fault, str(exc)))
                    with lock:
                        errors.append(f"{name}: {exc}")

            return worker

        for name, session in list(self.sessions.items()):
            panel = self.panels.get(name)
            if not panel:
                continue
            thread = threading.Thread(target=make_worker(name, session, panel), daemon=True)
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()
        if errors:
            self.tracking_stop_event.set()
            raise RuntimeError("; ".join(errors))
        return target

    def az_tracking_start_tolerance(self) -> float:
        return abs(self.site.az_track_tolerance_degrees)

    def az_tracking_stop_tolerance(self) -> float:
        return self.site.az_stop_tolerance_degrees

    def el_tracking_start_tolerance(self) -> float:
        return abs(self.site.el_track_tolerance_degrees)

    def el_tracking_stop_tolerance(self) -> float:
        return self.site.el_stop_tolerance_degrees

    def finish_target_slew(self, target: TargetPosition) -> None:
        self.apply_target_position(target)
        if not self.tracking_stop_event.is_set():
            self.status_var.set(f"Tracking {target.name}.")

    def finish_tracking_fault(self, message: str) -> None:
        self.tracking_stop_event.set()
        self.tracking_active = False
        self.tracking_kind = ""
        self.target_ha_var.set("HA --")
        self.status_var.set(f"Tracking fault: {message}")
        for panel in self.panels.values():
            if panel.session and panel.status_var.get() in ("SLEWING", "TRACKING"):
                panel.status_var.set("STOPPED")

    def refresh_tracking_target_display(self) -> None:
        if not self.tracking_active or not self.tracking_kind:
            return
        try:
            self.apply_target_position(self.current_tracking_target(self.tracking_kind))
        except Exception as exc:
            self.finish_tracking_fault(str(exc))

    def check_tracking_watchdog(self) -> None:
        if not self.tracking_active:
            return
        if self.tracking_thread and not self.tracking_thread.is_alive():
            self.tracking_active = False
            self.finish_tracking_fault("Tracking worker stopped unexpectedly.")
            return
        max_jog = max((session.config.limits.max_jog_seconds for session in self.sessions.values()), default=60.0)
        timeout = max(15.0, max_jog + 5.0, self.site.track_interval_seconds * 3.0 + 5.0)
        if time.monotonic() - self.tracking_last_update > timeout:
            self.tracking_stop_event.set()
            self.tracking_active = False
            self.finish_tracking_fault(f"Tracking worker stalled for more than {timeout:0.1f}s.")

    def kind_label(self, kind: str) -> str:
        if kind == "sun":
            return "Sun"
        if kind == "moon":
            return "Moon"
        if kind == "source":
            return self.site.selected_source or "Source"
        return kind

    def default_peak_cal_source_label(self) -> str:
        if self.tracking_kind == "moon":
            return "Moon"
        if self.tracking_kind == "source":
            return "Selected Source"
        return "Sun"

    def open_limits(self) -> None:
        if not self.configs:
            self.status_var.set("No antenna configs loaded.")
            return
        LimitsDialog(self)

    def open_observer(self) -> None:
        ObserverDialog(self)

    def open_tracking(self) -> None:
        if not self.configs:
            self.status_var.set("No antenna configs loaded.")
            return
        TrackingDialog(self)

    def open_sources(self) -> None:
        SourcesDialog(self)

    def open_calibration(self) -> None:
        if not self.configs:
            self.status_var.set("No antenna configs loaded.")
            return
        selected_name = (
            self.peak_calibration_dialog.antenna_var.get()
            if self.peak_calibration_dialog and self.peak_calibration_dialog.winfo_exists()
            else ""
        )
        if self.calibration_dialog and self.calibration_dialog.winfo_exists():
            self.calibration_dialog.refresh_offsets()
            self.calibration_dialog.refresh_live_positions()
            if selected_name:
                self.calibration_dialog.select_antenna(selected_name)
            self.calibration_dialog.lift()
            return
        self.calibration_dialog = CalibrationDialog(self)
        if selected_name:
            self.calibration_dialog.select_antenna(selected_name)

    def open_peak_calibration(self) -> None:
        if not self.configs:
            self.status_var.set("No antenna configs loaded.")
            return
        source_label = self.default_peak_cal_source_label()
        if self.peak_calibration_dialog and self.peak_calibration_dialog.winfo_exists():
            self.peak_calibration_dialog.set_source_label(source_label)
            self.peak_calibration_dialog.lift()
            return
        self.peak_calibration_dialog = PeakCalibrationDialog(self)

    def open_scan_calibration(self) -> None:
        if self.scan_dialog and self.scan_dialog.winfo_exists():
            self.scan_dialog.lift()
            return
        self.scan_dialog = ScanCalibrationDialog(self)

    def refresh_calibration_views(self, name: Optional[str] = None, position: Optional[Position] = None) -> None:
        if self.calibration_dialog and self.calibration_dialog.winfo_exists():
            self.calibration_dialog.refresh_offsets(name, position)
        if self.peak_calibration_dialog and self.peak_calibration_dialog.winfo_exists():
            self.peak_calibration_dialog.refresh_offsets(name, position)

    def select_calibration_antenna(self, name: str) -> None:
        if self.calibration_dialog and self.calibration_dialog.winfo_exists():
            self.calibration_dialog.select_antenna(name)

    def open_encoders(self) -> None:
        if not self.configs:
            self.status_var.set("No antenna configs loaded.")
            return
        EncodersDialog(self)

    def update_reference_positions(self) -> None:
        now_utc = datetime.now(timezone.utc)
        local_now = now_utc.astimezone()
        self.local_time_var.set(f"Local {local_now:%Y-%m-%d %H:%M:%S %Z}")
        self.utc_var.set(f"UTC {now_utc:%Y-%m-%d %H:%M:%S}")
        self.lmst_var.set(f"LMST {self.format_sidereal_time(local_sidereal_time(self.site.longitude, now_utc))}")
        try:
            sun = self.target_for_kind("sun", now_utc)
            moon = self.target_for_kind("moon", now_utc)
            self.sun_ref_var.set(f"Sun AZ {sun.azimuth:0.2f} EL {sun.elevation:0.2f}")
            self.moon_ref_var.set(f"Moon AZ {moon.azimuth:0.2f} EL {moon.elevation:0.2f}")
        except Exception as exc:
            self.sun_ref_var.set(f"Reference error: {exc}")
            self.moon_ref_var.set("")

    def format_sidereal_time(self, degrees: float) -> str:
        total_seconds = int(round((degrees / 15.0) * 3600.0)) % 86400
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def save_site_settings(self, message: str) -> None:
        save_site_config(self.config_path, self.site)
        self.status_var.set(message)

    def save_tracking_and_config(self, message: str) -> None:
        save_site_config(self.config_path, self.site)
        save_configs(self.config_path, self.configs)
        self.status_var.set(message)

    def stop_all(self) -> None:
        self.tracking_stop_event.set()
        self.park_stop_event.set()
        for panel in self.panels.values():
            panel.stop_event.set()
            if panel.session:
                panel.status_var.set("STOPPED")
        for session in self.sessions.values():
            self.run_worker(
                lambda s=session: (s.stop_all(), s.update_oled_activity("STOPPED")),
                lambda _result: None,
                self.set_status,
            )
        self.status_var.set("Stopped.")

    def periodic_refresh(self) -> None:
        if not self.tracking_active and not self.parking_active:
            self.refresh_all()
        elif self.tracking_active:
            self.refresh_tracking_target_display()
            self.check_tracking_watchdog()
        self.update_reference_positions()
        self.after(1500, self.periodic_refresh)

    def save_config(self, message: str = "Settings saved.") -> None:
        save_configs(self.config_path, self.configs)
        self.status_var.set(message)

    def run_worker(self, work, on_success, on_error) -> None:
        def target() -> None:
            try:
                self.events.put(("ok", on_success, work()))
            except Exception as exc:
                self.events.put(("error", on_error, str(exc)))

        threading.Thread(target=target, daemon=True).start()

    def process_events(self) -> None:
        while True:
            try:
                kind, callback, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind in ("ok", "position"):
                callback(payload)
            else:
                callback(str(payload))
        self.after(100, self.process_events)

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def on_close(self) -> None:
        try:
            self.power_panel.save_settings()
            self.power_panel.stop_log()
            self.power_panel.stop()
            self.stop_scan()
            self.stop_all()
            for session in self.sessions.values():
                session.close()
        finally:
            self.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch WT4 two-antenna GUI.")
    parser.add_argument("--config", default="wt4.ini", help="Config file. Default: wt4.ini")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = WT4App(args.config)
    try:
        app.mainloop()
    except KeyboardInterrupt:
        app.on_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
