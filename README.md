# WT_2 Two-Antenna Safety Controller

WT_2 is a GUI controller for two WinTrak Arduino/SVH3 antenna drive units.

It keeps the decoded serial protocol from WT_1, but adds the pieces needed
before automatic tracking is safe:

- one GUI controlling both antenna controllers
- persistent per-antenna configuration
- calibration offsets
- calibrated and raw position display
- software azimuth/elevation limits
- guarded jogs that poll during movement
- live GUI and OLED position updates during held jogs
- stop per antenna and stop all
- disconnect/reconnect from the serial controllers
- front-panel OLED updates with safety state instead of frequency

## Install

On the Raspberry Pi:

```bash
sudo apt update
sudo apt install -y python3-serial python3-tk
```

Copy and edit the config:

```bash
cp wt2.ini.example wt2.ini
nano wt2.ini
```

The antenna labels shown in the GUI and on the OLED come from the config
section names. The example uses:

```ini
[antenna:East]
[antenna:West]
```

The observer site and tracking settings are edited from the `Observer` and
`Tracking` buttons in the GUI and saved to the same config:

```ini
[site]
latitude = -32.724000
longitude = 152.130167
track_interval_seconds = 2.0
track_tolerance_degrees = 0.10
slow_speed = 20
slow_threshold_degrees = 3.0
```

Use stable device paths if available:

```bash
ls -l /dev/serial/by-id/
```

## Run

```bash
python3 wt2_gui.py
```

Or specify another config:

```bash
python3 wt2_gui.py --config wt2.ini
```

## First Use

1. Check `wt2.ini` ports and limits.
2. Start the GUI.
3. Press `Connect`.
4. Confirm raw and calibrated positions display for both antennas.
5. Confirm each controller OLED has populated with the current safety/status display.
6. Use guarded press-and-hold jogs only after confirming the displayed positions are sensible.
7. Press `Disconnect` before unplugging or changing controller wiring.

After disconnect, each antenna panel returns to its pre-connect blank position
state so old readings are not mistaken for live encoder data.

## Calibration

For each antenna:

1. Point the antenna to a known physical position.
2. Enter the actual AZ and EL in the antenna panel.
3. Press `Calibrate`.

The GUI reads the raw encoder positions and stores offsets in `wt2.ini`:

```ini
az_offset = ...
el_offset = ...
```

Status then shows both raw and calibrated positions. Software limits use the
calibrated position.

## Manual Control

Jog buttons are press-and-hold:

- movement starts when the button is pressed
- calibrated AZ/EL updates while the antenna is moving
- each controller OLED updates AZ/EL while the antenna is moving
- movement stops when the button is released
- movement also stops on any limit, encoder, serial, or watchdog fault

The speed field is persistent per antenna:

```ini
gui_speed = 40
```

Changing the speed text does not affect movement until `Enter`/`Return` is
pressed in the speed field. This prevents half-entered values becoming active.

The `Max jog` field is also persistent per antenna:

```ini
max_jog_seconds = 60.000
```

It is a held-button watchdog. Change it in the GUI and press `Enter`/`Return`
to save it. Existing `wt2.ini` files that still contain `5.000` will keep that
value until changed.

## Safety Limits

Each antenna has independent limits. Press `Limits` in the GUI to edit and save
these values without hand-editing `wt2.ini`:

```ini
az_min = 270.000
az_max = 265.000
el_min = 0.000
el_max = 87.000
az_margin = 0.500
el_margin = 0.500
max_jog_seconds = 60.000
poll_interval = 0.200
```

The default is 60 seconds. If a button-release event is missed, WT_2 stops the
axis when this time expires.

The Limits dialog validates numeric ranges before saving. New limits take effect
immediately for connected antennas and are written to `wt2.ini`.

Azimuth supports wrap-around. For example:

```ini
az_min = 270
az_max = 265
```

means the allowed range is:

```text
270 -> 360 and 0 -> 265
```

The GUI refuses a move if the current calibrated position is outside limits or
too close to the relevant limit. While a jog is active, it polls the encoder and
stops that axis if a safety check fails.

Because there are no physical limit switches, loss of encoder replies or any
protocol error is treated as a fault and movement stops.

## Sun Tracking

WT_2 includes a first-pass Sun tracking mode:

- `Track Sun` computes the current Sun AZ/EL and slews both connected antennas toward it.
- `Stop Track` stops tracking and sends stop commands.
- AZ and EL are allowed to slew concurrently on each antenna.
- Observer latitude/longitude are edited with `Observer`.
- `Speed`, `Max jog`, `Interval`, `Tolerance`, `Slow speed`, and `Slow deg` are edited with `Tracking`.

Sun tracking uses the same calibrated positions, software limits, margins, jog
speed, max-jog watchdog, encoder polling, and stop commands as manual movement.
If the Sun target is outside the configured safe limits, WT_2 stops instead of
moving.

Each antenna uses its own `Speed` value as the normal slew rate. When an axis is
within `Slow deg` of the target, WT_2 changes that axis to `Slow speed` until it
reaches the tracking tolerance.

`Interval` is limited to 0.1..10.0 seconds in 0.1 second steps. `Tolerance` is
limited to 0.01..0.20 degrees in 0.01 degree steps.

Use low speed for the first tests and confirm the displayed Sun AZ/EL is
reasonable before allowing larger slews.

## OLED Display

WT_2 writes the OLED over the decoded display command:

```text
F0/F1 35 column row length ASCII_TEXT 00
```

The display no longer shows frequency. That area is used for safety state:

```text
SAFE
FAULT
LIMIT
```

The OLED shows calibrated AZ/EL and raw encoder AZ/EL so each controller can be
checked without relying only on the Raspberry Pi screen.

## Not Yet Included

WT_2 does not yet include Moon, RA/Dec, catalogue, or full astronomical schedule
tracking. Those should come after Sun tracking is proven on both antennas.
