#!/usr/bin/env python3
"""WT_2 two-antenna safety/calibration GUI."""

from __future__ import annotations

import argparse
import queue
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta, timezone
from tkinter import messagebox, ttk
from typing import Optional

from wt2_astro import TargetPosition, moon_position, source_position
from wt2_config import (
    SiteConfig,
    SourceConfig,
    load_configs,
    load_site_config,
    load_sources,
    save_configs,
    save_site_config,
    save_sources,
)
from wt2_driver import AntennaConfig, Axis, Direction, EncoderInfo, Position, SafeAntenna, shortest_angle_delta
from wt2_solar import sun_position


class LimitsDialog(tk.Toplevel):
    def __init__(self, app: "WT2App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Antenna Limits")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.entries: dict[str, dict[str, tk.StringVar]] = {}

        tabs = ttk.Notebook(self)
        tabs.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")

        for name, config in app.configs.items():
            frame = ttk.Frame(tabs, padding=10)
            tabs.add(frame, text=name)
            self.entries[name] = self._build_limit_fields(frame, config)

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

    def save(self) -> None:
        parsed: dict[str, dict[str, float]] = {}
        try:
            for name, values in self.entries.items():
                parsed[name] = {key: float(value.get()) for key, value in values.items()}
                self._validate_limits(name, parsed[name])
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
            if name in self.app.panels:
                self.app.panels[name].sync_config_settings()

        self._format_fields(parsed)
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

    def _validate_limits(self, name: str, values: dict[str, float]) -> None:
        if not (0.0 <= values["az_min"] <= 360.0 and 0.0 <= values["az_max"] <= 360.0):
            raise RuntimeError(f"{name}: AZ limits must be 0..360 degrees.")
        if values["el_min"] >= values["el_max"]:
            raise RuntimeError(f"{name}: EL min must be less than EL max.")
        if not (-90.0 <= values["el_min"] <= 180.0 and -90.0 <= values["el_max"] <= 180.0):
            raise RuntimeError(f"{name}: EL limits must be -90..180 degrees.")
        if values["az_margin"] < 0.0 or values["el_margin"] < 0.0:
            raise RuntimeError(f"{name}: margins cannot be negative.")
        if not (1.0 <= values["max_jog_seconds"] <= 600.0):
            raise RuntimeError(f"{name}: max jog must be 1..600 seconds.")
        if not (0.05 <= values["poll_interval"] <= 5.0):
            raise RuntimeError(f"{name}: poll interval must be 0.05..5.0 seconds.")


class ObserverDialog(tk.Toplevel):
    def __init__(self, app: "WT2App") -> None:
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
    def __init__(self, app: "WT2App") -> None:
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

        body = ttk.Frame(self, padding=10)
        body.grid(row=0, column=0, sticky="nsew")
        self.tree = ttk.Treeview(body, columns=("ra", "dec", "flux"), show="headings", height=7)
        self.tree.heading("ra", text="RA h")
        self.tree.heading("dec", text="Dec deg")
        self.tree.heading("flux", text="4800 MHz")
        self.tree.column("ra", width=80, anchor="e")
        self.tree.column("dec", width=80, anchor="e")
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

    def _field(self, parent: ttk.Frame, label: str, variable: tk.StringVar, row: int, width: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=variable, width=width).grid(row=row, column=1, sticky="w", pady=2)

    def refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for name in sorted(self.sources):
            source = self.sources[name]
            self.tree.insert("", "end", iid=name, values=(f"{source.ra_hours:0.6f}", f"{source.dec_degrees:0.4f}", f"{source.flux_4800_mhz:0.1f}"))
        if self.app.site.selected_source in self.sources:
            self.tree.selection_set(self.app.site.selected_source)
            self.tree.focus(self.app.site.selected_source)

    def load_selected(self, _event: Optional[object] = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        source = self.sources[selection[0]]
        self.name_var.set(source.name)
        self.ra_var.set(f"{source.ra_hours:0.6f}")
        self.dec_var.set(f"{source.dec_degrees:0.4f}")
        self.flux_var.set(f"{source.flux_4800_mhz:0.1f}")

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
        old_selection = self.tree.selection()
        if old_selection and old_selection[0] != source.name:
            self.sources.pop(old_selection[0], None)
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
        self.save_to_app("Sources saved.")
        self.destroy()

    def save_to_app(self, message: str) -> None:
        self.app.sources = self.sources
        save_sources(self.app.config_path, self.app.sources, self.app.site.selected_source)
        self.app.save_site_settings(message)
        self.app.refresh_source_status()

    def validate_source(self, source: SourceConfig) -> None:
        if not (0.0 <= source.ra_hours < 24.0):
            raise RuntimeError("RA must be 0.0 <= RA < 24.0 hours.")
        if not (-90.0 <= source.dec_degrees <= 90.0):
            raise RuntimeError("Dec must be -90..90 degrees.")
        if source.flux_4800_mhz < 0.0:
            raise RuntimeError("Flux cannot be negative.")


class CalibrationDialog(tk.Toplevel):
    def __init__(self, app: "WT2App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Calibration")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.entries: dict[str, tuple[tk.StringVar, tk.StringVar]] = {}

        tabs = ttk.Notebook(self)
        tabs.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
        for name, panel in app.panels.items():
            frame = ttk.Frame(tabs, padding=10)
            tabs.add(frame, text=name)
            az_var = tk.StringVar()
            el_var = tk.StringVar()
            if panel.session and panel.session.last_position:
                az_var.set(f"{panel.session.last_position.azimuth:0.2f}")
                el_var.set(f"{panel.session.last_position.elevation:0.2f}")
            ttk.Label(frame, text="Actual AZ").grid(row=0, column=0, sticky="w", pady=2)
            ttk.Entry(frame, textvariable=az_var, width=8).grid(row=0, column=1, sticky="w", pady=2)
            ttk.Label(frame, text="Actual EL").grid(row=1, column=0, sticky="w", pady=2)
            ttk.Entry(frame, textvariable=el_var, width=8).grid(row=1, column=1, sticky="w", pady=2)
            ttk.Button(frame, text="Calibrate", command=lambda n=name: self.calibrate(n)).grid(
                row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0)
            )
            self.entries[name] = (az_var, el_var)

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Close", command=self.destroy).grid(row=0, column=1)

    def calibrate(self, name: str) -> None:
        panel = self.app.panels.get(name)
        if not panel or not panel.session:
            messagebox.showerror("Calibration", f"{name} is not connected.", parent=self)
            return
        az_var, el_var = self.entries[name]
        try:
            actual_az = float(az_var.get())
            actual_el = float(el_var.get())
        except ValueError:
            messagebox.showerror("Calibration", "Actual AZ and EL must be numeric.", parent=self)
            return
        if not (0.0 <= actual_az <= 360.0 and -90.0 <= actual_el <= 180.0):
            messagebox.showerror("Calibration", "Calibration AZ must be 0..360 and EL -90..180.", parent=self)
            return

        def work() -> Position:
            position = panel.session.calibrate(actual_az, actual_el)
            self.app.save_config("Calibration saved.")
            panel.session.update_oled("CAL")
            return position

        self.app.run_worker(
            work,
            lambda position, p=panel, av=az_var, ev=el_var: self.finish_calibration(p, av, ev, position),
            lambda text: messagebox.showerror("Calibration", text, parent=self),
        )

    def finish_calibration(
        self,
        panel: "AntennaPanel",
        az_var: tk.StringVar,
        el_var: tk.StringVar,
        position: Position,
    ) -> None:
        az_var.set(f"{position.azimuth:0.2f}")
        el_var.set(f"{position.elevation:0.2f}")
        panel.clear_message()
        panel.update_position(position)


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

    def __init__(self, app: "WT2App") -> None:
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
        if axis == Axis.ELEVATION and not (0.0 <= position <= 180.0):
            self.show_error("EL Arduino position must be 0..180 degrees.")
            return
        if not messagebox.askyesno(
            "Set Encoder Position",
            f"Set {name} {axis_label} Arduino position to {position:0.2f}?\n\n"
            "This resets the WT_2 software calibration offset for this axis to zero.",
            parent=self,
        ):
            return

        def work() -> Position:
            updated = session.set_encoder_position(axis, position)
            self.app.save_config("Encoder position saved.")
            session.update_oled("CAL")
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
    def __init__(self, app: "WT2App") -> None:
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
        self.az_slow_speed_var = tk.StringVar(value=str(app.site.az_slow_speed))
        self.el_slow_speed_var = tk.StringVar(value=str(app.site.el_slow_speed))
        self.az_slow_threshold_var = tk.StringVar(value=f"{app.site.az_slow_threshold_degrees:0.1f}")
        self.el_slow_threshold_var = tk.StringVar(value=f"{app.site.el_slow_threshold_degrees:0.1f}")

        body = ttk.Frame(self, padding=10)
        body.grid(row=0, column=0, sticky="nsew")
        self._spin_field(body, "Interval sec", self.interval_var, 0, 0.1, 10.0, 0.1, width=7)

        ttk.Separator(body, orient="horizontal").grid(row=1, column=0, columnspan=5, sticky="ew", pady=8)
        ttk.Label(body, text="Axis").grid(row=2, column=0, sticky="w")
        ttk.Label(body, text="Tolerance").grid(row=2, column=1, sticky="w")
        ttk.Label(body, text="Slow speed").grid(row=2, column=2, sticky="w")
        ttk.Label(body, text="Slow deg").grid(row=2, column=3, sticky="w")
        ttk.Label(body, text="AZ").grid(row=3, column=0, sticky="w", pady=2)
        self._spin_only(body, self.az_tolerance_var, 3, 1, -0.20, 0.20, 0.01, width=7)
        ttk.Entry(body, textvariable=self.az_slow_speed_var, width=7).grid(row=3, column=2, sticky="w", pady=2)
        ttk.Entry(body, textvariable=self.az_slow_threshold_var, width=7).grid(row=3, column=3, sticky="w", pady=2)
        ttk.Label(body, text="EL").grid(row=4, column=0, sticky="w", pady=2)
        self._spin_only(body, self.el_tolerance_var, 4, 1, -0.20, 0.20, 0.01, width=7)
        ttk.Entry(body, textvariable=self.el_slow_speed_var, width=7).grid(row=4, column=2, sticky="w", pady=2)
        ttk.Entry(body, textvariable=self.el_slow_threshold_var, width=7).grid(row=4, column=3, sticky="w", pady=2)

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
                track_interval_seconds=round(float(self.interval_var.get()), 1),
                az_track_tolerance_degrees=round(float(self.az_tolerance_var.get()), 2),
                el_track_tolerance_degrees=round(float(self.el_tolerance_var.get()), 2),
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
    def __init__(self, master: tk.Misc, app: "WT2App", name: str, config: Optional[AntennaConfig] = None) -> None:
        super().__init__(master, padding=8)
        self.app = app
        self.name = name
        self.config = config
        self.session: Optional[SafeAntenna] = None
        self.stop_event = threading.Event()

        self.status_var = tk.StringVar(value="DISCONNECTED")
        self.raw_az_var = tk.StringVar(value="--")
        self.raw_el_var = tk.StringVar(value="--")
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
        for col in range(4):
            position_frame.columnconfigure(col, weight=1)
        self._position_cell(position_frame, 0, 0, "Raw AZ", self.raw_az_var)
        self._position_cell(position_frame, 0, 2, "Raw EL", self.raw_el_var)
        self._position_cell(position_frame, 1, 0, "Cal AZ", self.cal_az_var)
        self._position_cell(position_frame, 1, 2, "Cal EL", self.cal_el_var)

        control = ttk.LabelFrame(self, text="Manual")
        control.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        for col in range(3):
            control.columnconfigure(col, weight=1)
        self._hold_button(control, "EL+", Direction.EL_UP).grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        self._hold_button(control, "AZ-", Direction.AZ_CCW).grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(control, text="STOP", command=self.stop).grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        self._hold_button(control, "AZ+", Direction.AZ_CW).grid(row=1, column=2, sticky="ew", padx=2, pady=2)
        self._hold_button(control, "EL-", Direction.EL_DOWN).grid(row=2, column=1, sticky="ew", padx=2, pady=2)

        ttk.Label(self, textvariable=self.fault_var, foreground="red", wraplength=260).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0)
        )

    def _position_cell(self, parent: tk.Misc, row: int, column: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=(0, 4))
        ttk.Label(parent, textvariable=variable, font=("TkDefaultFont", 11, "bold")).grid(
            row=row, column=column + 1, sticky="e", padx=(0, 8)
        )

    def _hold_button(self, master: tk.Misc, text: str, direction: Direction) -> ttk.Button:
        button = ttk.Button(master, text=text)
        button.bind("<ButtonPress-1>", lambda _event: self.start_jog(direction))
        button.bind("<ButtonRelease-1>", lambda _event: self.stop())
        button.bind("<Leave>", lambda _event: self.stop())
        return button

    def attach(self, session: SafeAntenna) -> None:
        self.session = session
        self.sync_config_settings()
        self.status_var.set("CONNECTED")
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
        self.raw_az_var.set("--")
        self.raw_el_var.set("--")
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
        self.raw_az_var.set(f"{position.raw_azimuth:0.2f}")
        self.raw_el_var.set(f"{position.raw_elevation:0.2f}")
        self.cal_az_var.set(f"{position.azimuth:0.2f}")
        self.cal_el_var.set(f"{position.elevation:0.2f}")

    def set_fault(self, text: str) -> None:
        self.fault_var.set(text)
        self.status_var.set("FAULT" if text else "CONNECTED")

    def set_tracking_status(self, text: str) -> None:
        if self.session and not self.fault_var.get():
            self.status_var.set(text)

    def set_message(self, text: str) -> None:
        self.fault_var.set(text)
        if self.session:
            self.status_var.set("CONNECTED")

    def clear_message(self) -> None:
        self.fault_var.set("")
        if self.session:
            self.status_var.set("CONNECTED")

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
            session.update_oled_position()

        def work() -> Position:
            session.guarded_jog(direction, speed, None, self.stop_event, realtime_update)
            position = session.read_position()
            session.update_oled("MANUAL")
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


class WT2App(tk.Tk):
    def __init__(self, config_path: str) -> None:
        super().__init__()
        self.title("WT_2 Antenna Controller")
        self.geometry("840x500")
        self.config_path = config_path
        self.configs = load_configs(config_path)
        self.site = load_site_config(config_path)
        self.sources = load_sources(config_path)
        self.sessions: dict[str, SafeAntenna] = {}
        self.events: queue.Queue[tuple[str, object, object]] = queue.Queue()
        self.tracking_stop_event = threading.Event()
        self.tracking_active = False
        self.tracking_kind = ""
        self.target_name_var = tk.StringVar(value="Target --")
        self.target_az_var = tk.StringVar(value="AZ --")
        self.target_el_var = tk.StringVar(value="EL --")
        self.sun_ref_var = tk.StringVar(value="Sun AZ -- EL --")
        self.moon_ref_var = tk.StringVar(value="Moon AZ -- EL --")
        self.source_status_var = tk.StringVar()

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
        ttk.Button(top_row_1, text="Encoders", command=self.open_encoders).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="STOP ALL", command=self.stop_all).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_2, text="Track Sun", command=lambda: self.start_tracking("sun")).pack(side="left")
        ttk.Button(top_row_2, text="Track Moon", command=lambda: self.start_tracking("moon")).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_2, text="Track Source", command=lambda: self.start_tracking("source")).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_2, text="Stop Track", command=self.stop_sun_tracking).pack(side="left", padx=(6, 0))

        target_bar = ttk.Frame(self, padding=(8, 0, 8, 2))
        target_bar.pack(fill="x")
        ttk.Label(target_bar, textvariable=self.target_name_var).pack(side="left")
        ttk.Label(target_bar, textvariable=self.target_az_var).pack(side="left", padx=(16, 0))
        ttk.Label(target_bar, textvariable=self.target_el_var).pack(side="left", padx=(16, 0))
        ttk.Label(target_bar, textvariable=self.source_status_var).pack(side="left", padx=(16, 0))
        reference_bar = ttk.Frame(self, padding=(8, 0, 8, 2))
        reference_bar.pack(fill="x")
        ttk.Label(reference_bar, textvariable=self.sun_ref_var).pack(side="left")
        ttk.Label(reference_bar, textvariable=self.moon_ref_var).pack(side="left", padx=(16, 0))

        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        self.panels: dict[str, AntennaPanel] = {}
        names = list(self.configs) or ["antenna_a", "antenna_b"]
        for index, name in enumerate(names[:2]):
            panel = AntennaPanel(body, self, name, self.configs.get(name))
            panel.grid(row=0, column=index, sticky="nsew", padx=4)
            self.panels[name] = panel

        if not self.configs:
            self.status_var.set(f"No antennas found in {config_path}. Copy wt2.ini.example to wt2.ini.")
        self.refresh_source_status()

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
        session.update_oled("MANUAL")
        return session

    def attach_session(self, name: str, session: SafeAntenna) -> None:
        self.sessions[name] = session
        if name in self.panels:
            self.panels[name].attach(session)
        connected = len(self.sessions)
        total = len(self.configs)
        self.status_var.set(f"Connected {connected}/{total} antennas. Guarded manual mode ready.")

    def disconnect_all(self) -> None:
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
            self.status_var.set(f"Connected {connected}/{total} antennas. Guarded manual mode ready.")
        else:
            self.status_var.set("Disconnected.")

    def refresh_all(self) -> None:
        for panel in self.panels.values():
            panel.refresh()

    def oled_all(self) -> None:
        for session in self.sessions.values():
            self.run_worker(lambda s=session: s.update_oled("MANUAL"), lambda _result: None, self.set_status)

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
        self.status_var.set(f"Slewing to {self.kind_label(kind)}.")
        threading.Thread(target=lambda: self.tracking_loop(kind), daemon=True).start()

    def stop_sun_tracking(self) -> None:
        self.tracking_stop_event.set()
        self.tracking_active = False
        self.stop_all()
        self.status_var.set("Tracking stopped.")

    def tracking_loop(self, kind: str) -> None:
        acquired = False
        try:
            while not self.tracking_stop_event.is_set():
                target = self.current_tracking_target(kind)
                self.events.put(("ok", self.apply_target_position, target))
                if not acquired:
                    self.events.put(("ok", self.set_status, f"Slewing to {target.name}."))
                self.slew_all_to_target(target, target.name[:8].upper(), show_slewing=not acquired)
                acquired = True
                self.events.put(("ok", self.finish_target_slew, target))
                wait_until = time.monotonic() + max(0.1, self.site.track_interval_seconds)
                while not self.tracking_stop_event.is_set() and time.monotonic() < wait_until:
                    time.sleep(0.05)
        except Exception as exc:
            self.events.put(("error", self.set_status, str(exc)))
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
        return TargetPosition(
            name=source.name,
            azimuth=azimuth,
            elevation=elevation,
        )

    def validate_site(self, site: SiteConfig) -> None:
        self.validate_observer(site)
        if not (0.1 <= site.track_interval_seconds <= 10.0):
            raise RuntimeError("Tracking interval must be 0.1..10.0 seconds.")
        self._validate_axis_tracking("AZ", site.az_track_tolerance_degrees, site.az_slow_speed, site.az_slow_threshold_degrees)
        self._validate_axis_tracking("EL", site.el_track_tolerance_degrees, site.el_slow_speed, site.el_slow_threshold_degrees)

    def _validate_axis_tracking(self, axis: str, tolerance: float, slow_speed: int, slow_threshold: float) -> None:
        if not (-0.2 <= tolerance <= 0.2) or tolerance == 0.0:
            raise RuntimeError(f"{axis} tolerance must be -0.20..-0.01 or 0.01..0.20 degrees.")
        if not (1 <= slow_speed <= 100):
            raise RuntimeError(f"{axis} slow speed must be 1..100.")
        if not (abs(tolerance) <= slow_threshold <= 30.0):
            raise RuntimeError(f"{axis} slow deg must be at least tolerance and no more than 30 degrees.")

    def validate_observer(self, site: SiteConfig) -> None:
        if not (-90.0 <= site.latitude <= 90.0):
            raise RuntimeError("Latitude must be -90..90 degrees.")
        if not (-180.0 <= site.longitude <= 180.0):
            raise RuntimeError("Longitude must be -180..180 degrees.")

    def apply_target_position(self, target: TargetPosition) -> None:
        self.target_name_var.set(target.name)
        self.target_az_var.set(f"AZ {target.azimuth:0.2f}")
        self.target_el_var.set(f"EL {target.elevation:0.2f}")

    def slew_all_to_target(self, target: TargetPosition, mode: str, show_slewing: bool = True) -> TargetPosition:
        errors: list[str] = []
        threads: list[threading.Thread] = []
        lock = threading.Lock()

        def make_worker(name: str, session: SafeAntenna, panel: AntennaPanel):
            def progress(position: Position) -> None:
                self.events.put(("position", panel.update_position, position))
                session.update_oled_position(target.azimuth, target.elevation)

            def worker() -> None:
                try:
                    self.events.put(("ok", panel.set_tracking_status, "SLEWING" if show_slewing else "TRACKING"))
                    position = session.guarded_slew_to(
                        target.azimuth,
                        target.elevation,
                        session.config.az_track_speed,
                        session.config.el_track_speed,
                        self.tracking_stop_event,
                        self.az_tracking_stop_tolerance(),
                        self.el_tracking_stop_tolerance(),
                        self.site.az_slow_speed,
                        self.site.el_slow_speed,
                        self.site.az_slow_threshold_degrees,
                        self.site.el_slow_threshold_degrees,
                        progress,
                    )
                    session.update_oled(mode, target.azimuth, target.elevation)
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

    def az_tracking_stop_tolerance(self) -> float:
        if self.site.az_track_tolerance_degrees < 0:
            return 0.01
        return abs(self.site.az_track_tolerance_degrees)

    def el_tracking_stop_tolerance(self) -> float:
        if self.site.el_track_tolerance_degrees < 0:
            return 0.01
        return abs(self.site.el_track_tolerance_degrees)

    def finish_target_slew(self, target: TargetPosition) -> None:
        self.apply_target_position(target)
        if not self.tracking_stop_event.is_set():
            self.status_var.set(f"Tracking {target.name}.")

    def kind_label(self, kind: str) -> str:
        if kind == "sun":
            return "Sun"
        if kind == "moon":
            return "Moon"
        if kind == "source":
            return self.site.selected_source or "Source"
        return kind

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
        CalibrationDialog(self)

    def open_encoders(self) -> None:
        if not self.configs:
            self.status_var.set("No antenna configs loaded.")
            return
        EncodersDialog(self)

    def refresh_source_status(self) -> None:
        if self.site.selected_source and self.site.selected_source in self.sources:
            source = self.sources[self.site.selected_source]
            self.source_status_var.set(
                f"Source {source.name} RA {source.ra_hours:0.4f} Dec {source.dec_degrees:0.2f} Flux {source.flux_4800_mhz:0.1f}"
            )
        else:
            self.source_status_var.set("Source none")

    def update_reference_positions(self) -> None:
        try:
            sun = self.target_for_kind("sun")
            moon = self.target_for_kind("moon")
            self.sun_ref_var.set(f"Sun AZ {sun.azimuth:0.2f} EL {sun.elevation:0.2f}")
            self.moon_ref_var.set(f"Moon AZ {moon.azimuth:0.2f} EL {moon.elevation:0.2f}")
        except Exception as exc:
            self.sun_ref_var.set(f"Reference error: {exc}")
            self.moon_ref_var.set("")

    def save_site_settings(self, message: str) -> None:
        save_site_config(self.config_path, self.site)
        self.status_var.set(message)

    def save_tracking_and_config(self, message: str) -> None:
        save_site_config(self.config_path, self.site)
        save_configs(self.config_path, self.configs)
        self.status_var.set(message)

    def stop_all(self) -> None:
        self.tracking_stop_event.set()
        for panel in self.panels.values():
            panel.stop_event.set()
            if panel.session:
                panel.status_var.set("CONNECTED")
        for session in self.sessions.values():
            self.run_worker(lambda s=session: s.stop_all(), lambda _result: None, self.set_status)
        self.status_var.set("Stop commands sent.")

    def periodic_refresh(self) -> None:
        if not self.tracking_active:
            self.refresh_all()
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
            self.stop_all()
            for session in self.sessions.values():
                session.close()
        finally:
            self.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch WT_2 two-antenna GUI.")
    parser.add_argument("--config", default="wt2.ini", help="Config file. Default: wt2.ini")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = WT2App(args.config)
    try:
        app.mainloop()
    except KeyboardInterrupt:
        app.on_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
