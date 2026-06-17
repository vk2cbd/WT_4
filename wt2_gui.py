#!/usr/bin/env python3
"""WT_2 two-antenna safety/calibration GUI."""

from __future__ import annotations

import argparse
import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import Optional

from wt2_config import load_configs, save_configs
from wt2_driver import AntennaConfig, Direction, Position, SafeAntenna


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
        self.actual_az_var = tk.StringVar()
        self.actual_el_var = tk.StringVar()

        initial_speed = config.gui_speed if config else 40
        self.speed_value = initial_speed
        self.speed_var = tk.StringVar(value=str(initial_speed))
        self.jog_thread_active = False

        self.columnconfigure(1, weight=1)
        ttk.Label(self, text=name.upper(), font=("TkDefaultFont", 13, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(self, textvariable=self.status_var).grid(row=0, column=1, sticky="e")

        self._label_pair(1, "Raw AZ", self.raw_az_var)
        self._label_pair(2, "Raw EL", self.raw_el_var)
        self._label_pair(3, "Cal AZ", self.cal_az_var)
        self._label_pair(4, "Cal EL", self.cal_el_var)

        ttk.Label(self, text="Actual AZ").grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(self, textvariable=self.actual_az_var, width=10).grid(row=5, column=1, sticky="ew", pady=(8, 0))
        ttk.Label(self, text="Actual EL").grid(row=6, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.actual_el_var, width=10).grid(row=6, column=1, sticky="ew")
        ttk.Button(self, text="Calibrate", command=self.calibrate).grid(row=7, column=0, columnspan=2, sticky="ew", pady=(4, 8))

        control = ttk.Frame(self)
        control.grid(row=8, column=0, columnspan=2, sticky="ew")
        for col in range(3):
            control.columnconfigure(col, weight=1)
        self._hold_button(control, "AZ CCW", Direction.AZ_CCW).grid(row=0, column=0, sticky="ew")
        self._hold_button(control, "AZ CW", Direction.AZ_CW).grid(row=0, column=2, sticky="ew")
        self._hold_button(control, "EL UP", Direction.EL_UP).grid(row=1, column=1, sticky="ew")
        self._hold_button(control, "EL DOWN", Direction.EL_DOWN).grid(row=2, column=1, sticky="ew")

        settings = ttk.Frame(self)
        settings.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(settings, text="Speed").grid(row=0, column=0, sticky="w")
        speed_entry = ttk.Entry(settings, textvariable=self.speed_var, width=5)
        speed_entry.grid(row=0, column=1, sticky="w")
        speed_entry.bind("<Return>", self.commit_speed)
        ttk.Label(settings, text="press Enter to commit").grid(row=0, column=2, sticky="w", padx=(8, 0))

        ttk.Button(self, text="STOP", command=self.stop).grid(row=10, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(self, textvariable=self.fault_var, foreground="red", wraplength=260).grid(
            row=11, column=0, columnspan=2, sticky="ew", pady=(6, 0)
        )

    def _label_pair(self, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w")
        ttk.Label(self, textvariable=variable, font=("TkDefaultFont", 11, "bold")).grid(row=row, column=1, sticky="e")

    def _hold_button(self, master: tk.Misc, text: str, direction: Direction) -> ttk.Button:
        button = ttk.Button(master, text=text)
        button.bind("<ButtonPress-1>", lambda _event: self.start_jog(direction))
        button.bind("<ButtonRelease-1>", lambda _event: self.stop())
        button.bind("<Leave>", lambda _event: self.stop())
        return button

    def attach(self, session: SafeAntenna) -> None:
        self.session = session
        self.speed_value = session.config.gui_speed
        self.speed_var.set(str(self.speed_value))
        self.status_var.set("CONNECTED")
        self.update_position(session.last_position)

    def detach(self) -> None:
        self.stop_event.set()
        self.session = None
        self.jog_thread_active = False
        self.status_var.set("DISCONNECTED")
        self.fault_var.set("")

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

    def refresh(self) -> None:
        if not self.session:
            return
        self.app.run_worker(lambda: self.session.read_position(), self.update_position, self.set_fault)

    def calibrate(self) -> None:
        if not self.session:
            return
        try:
            actual_az = float(self.actual_az_var.get())
            actual_el = float(self.actual_el_var.get())
        except ValueError:
            self.set_fault("Calibration requires numeric actual AZ and EL.")
            return

        def work() -> Position:
            position = self.session.calibrate(actual_az, actual_el)
            self.app.save_config("Calibration saved.")
            self.session.update_oled("CAL")
            return position

        self.app.run_worker(work, self.update_position, self.set_fault)

    def commit_speed(self, _event: Optional[object] = None) -> bool:
        try:
            value = max(0, min(100, int(self.speed_var.get())))
        except ValueError:
            self.speed_var.set(str(self.speed_value))
            self.set_fault("Speed must be a whole number from 0 to 100.")
            return False
        self.speed_value = value
        self.speed_var.set(str(value))
        config = self.session.config if self.session else self.config
        if config:
            config.gui_speed = value
            self.app.save_config("Settings saved.")
        self.set_fault("")
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

    def finish_jog(self, position: Position) -> None:
        self.jog_thread_active = False
        self.update_position(position)

    def finish_jog_fault(self, text: str) -> None:
        self.jog_thread_active = False
        self.set_fault(text)

    def stop(self) -> None:
        self.stop_event.set()
        if self.session:
            self.app.run_worker(lambda: self.session.stop_all(), lambda _result: None, self.set_fault)


class WT2App(tk.Tk):
    def __init__(self, config_path: str) -> None:
        super().__init__()
        self.title("WT_2 Antenna Controller")
        self.geometry("760x520")
        self.config_path = config_path
        self.configs = load_configs(config_path)
        self.sessions: dict[str, SafeAntenna] = {}
        self.events: queue.Queue[tuple[str, object, object]] = queue.Queue()

        self.status_var = tk.StringVar(value="Load config, connect antennas, then use guarded jogs.")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Button(top, text="Connect", command=self.connect_all).pack(side="left")
        ttk.Button(top, text="Disconnect", command=self.disconnect_all).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Refresh All", command=self.refresh_all).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="OLED All", command=self.oled_all).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="STOP ALL", command=self.stop_all).pack(side="right")

        ttk.Label(self, textvariable=self.status_var, padding=(8, 2)).pack(fill="x")

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

        self.after(100, self.process_events)
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

    def stop_all(self) -> None:
        for panel in self.panels.values():
            panel.stop_event.set()
        for session in self.sessions.values():
            self.run_worker(lambda s=session: s.stop_all(), lambda _result: None, self.set_status)
        self.status_var.set("Stop commands sent.")

    def periodic_refresh(self) -> None:
        self.refresh_all()
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
