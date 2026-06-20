# WT4 Two-Antenna Safety Controller

WT4 is a GUI controller for two WinTrak Arduino/SVH3 antenna drive units.

It keeps the decoded serial protocol from WT_1, but adds the pieces needed
before automatic tracking is safe:

- one GUI controlling both antenna controllers
- persistent per-antenna configuration
- calibration offsets
- calibrated position display, with raw encoder positions available in Calibration
- software azimuth/elevation limits
- guarded jogs that poll during movement
- live GUI and OLED position updates during held jogs
- stop per antenna and stop all
- disconnect/reconnect from the serial controllers
- front-panel OLED updates with safety state instead of frequency
- a separate power-meter module boundary ready for RTL-SDR integration

## Install

On the Raspberry Pi:

```bash
sudo apt update
sudo apt install -y python3-serial python3-tk rtl-sdr librtlsdr0
```

Copy and edit the config:

```bash
cp wt4.ini.example wt4.ini
nano wt4.ini
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
selected_source = Virgo A
track_interval_seconds = 2.0
az_track_tolerance_degrees = 0.10
el_track_tolerance_degrees = 0.10
az_stop_tolerance_degrees = 0.10
el_stop_tolerance_degrees = 0.10
az_slow_speed = 20
el_slow_speed = 20
az_slow_threshold_degrees = 3.0
el_slow_threshold_degrees = 3.0
```

User radio sources are edited from the `Sources` button and saved as config
sections:

```ini
[source:Virgo A]
ra_hours = 12.513700
dec_degrees = 12.391100
flux_4800_mhz = 70.000
```

Use stable device paths if available:

```bash
ls -l /dev/serial/by-id/
```

## Run

```bash
python3 wt4_gui.py
```

Or specify another config:

```bash
python3 wt4_gui.py --config wt4.ini
```

## RTL Power Meter

WT4 starts the RTL power-meter work as a separate subsystem rather than adding
SDR code directly into the antenna GUI.

The first hardware test is command-line only:

```bash
python3 wt4_power_cli.py --freq 1200000000 --rate 1024000 --gain 29.7 --samples 0
```

Use `--count` for a short test:

```bash
python3 wt4_power_cli.py --freq 1200000000 --rate 1024000 --gain 29.7 --samples 0 --count 20
```

If your tuner works better in automatic gain mode, omit `--gain`.
Use `--samples 0` to calculate samples/read from sample rate divided by update
rate. For example, 1024000 sps at 10 Hz uses about 102400 IQ samples per power
reading. Larger manual sample values give more averaging; smaller values give
faster response. Many RTL-SDR drivers reject sample rates between 300000 and
900001 sps, so 524288 sps may fail even though it is a useful bandwidth target.

The power-meter module currently provides:

- power-meter configuration defaults
- a 10 Hz update model
- a 1024000 sps sample-rate default known to work with common RTL-SDR drivers
- a relative dBFS power calculation helper
- synchronous RTL-SDR sample capture using `librtlsdr`

The main GUI also includes a compact RTL Power Meter panel. Set frequency in
MHz, sample rate in ksps, gain, samples/read in kilosamples, GUI refresh rate, averaging, and
warm-up seconds, then press `Start Power`. `Sample ksps` sets the RTL sample rate and
therefore the approximate RF bandwidth accepted by the dongle/driver. `GUI Hz`
sets the desired power display refresh rate. Set `Samples` to `auto` or `0` to calculate samples/read from
the converted sample rate divided by `GUI Hz`. The displayed value is relative dBFS; less-negative
values mean stronger received power. Power-meter settings are saved in
`wt4.ini` when the meter starts or the GUI closes. During warm-up the live
reading is shown, but the status remains `Warming` until the configured settling
time has elapsed, then changes to `Ready`.

Use `Start Log` to write a timestamped `wt4_power_YYYYMMDD-HHMMSS.csv` file
containing power, target position, and the latest antenna positions. This is the
first step toward source sweep calibration; it is passive and does not command
the antennas.

`Scan Cal` performs a source-relative calibration scan while normal tracking
continues. Start tracking Sun, Moon, or a selected source, start the RTL power
meter, open `Scan Cal`, choose the antenna, span, increment, dwell, and number
of scans, then run an AZ or EL scan. WT4 offsets only the selected antenna's
live tracking target by each scan point while the other antenna remains on the
nominal source, waits for the guarded tracking move, and averages power during
the dwell. Repeated scans always run from `-span` to `+span`; matching offset
points are averaged before plotting. The raw and averaged measurements are saved
to `wt4_scan_<antenna>_az_*.csv` or `wt4_scan_<antenna>_el_*.csv`, and a graph
of averaged power versus scan offset opens when complete. The graph includes a
vertical boresight line at zero offset and an automatic Gaussian fit with a
linear baseline term. The fit summary reports centre offset, FWHM, fitted peak,
and RMS residual. Scan defaults are stored in the `[scan]` section of `wt4.ini`.

## First Use

1. Check `wt4.ini` ports and limits.
2. Start the GUI.
3. Press `Connect`.
4. Confirm calibrated positions display for both antennas.
5. Confirm each controller OLED has populated with the current safety/status display.
6. Use guarded press-and-hold jogs only after confirming the displayed positions are sensible.
7. Press `Disconnect` before unplugging or changing controller wiring.

After disconnect, each antenna panel returns to its pre-connect blank position
state so old readings are not mistaken for live encoder data.

## Calibration

For each antenna:

1. Point the antenna to a known physical position.
2. Press `Calibration`.
3. Enter the actual AZ and EL in the antenna tab.
4. Press `Calibrate Manual`.

The GUI reads the raw encoder positions and stores offsets in `wt4.ini`:

```ini
az_offset = ...
el_offset = ...
```

The main antenna panels show calibrated AZ/EL. The Calibration menu shows raw
encoder positions and offset values. Software limits use the calibrated
position.

The Calibration menu also shows the current AZ/EL offsets. These can be edited
directly and applied with `Apply Offsets`.

`Calibrate From Target` uses the current target shown on the main screen, such
as Sun, Moon, or the active tracked source, and associates that target AZ/EL
with the antenna's current physical pointing. Calibration AZ must be 0..360
degrees and calibration EL must be 0..90 degrees.

`Peak Cal` is intended for source-peaking calibration where timing matters. It
keeps one axis tracking while the other axis is manually jogged to maximum
source power, then locks only the manually peaked axis with one button press:

1. Select `Sun`, `Moon`, or `Selected Source`.
2. Select the antenna.
3. Use `Track AZ Only` when manually peaking EL, or `Track EL Only` when
   manually peaking AZ.
4. Jog the untracked axis until the received power is maximised.
5. Press `LOCK EL CAL` or `LOCK AZ CAL`.

The lock button reads the raw encoder position and source coordinate at that
moment, updates only the selected axis offset, saves `wt4.ini`, and leaves the
other axis offset unchanged.

## Encoder Scan

Press `Encoders` to scan the Arduino encoder configuration for each connected
antenna. The dialog shows the decoded SEI metadata, current Arduino-held
position, resolution, and mode for AZ and EL.

The `Position` field is editable. Press `Set` on a row to write the displayed
position into the Arduino for that axis using the decoded WinTrak command:

```text
F0 02 HH LL   set AZ Arduino position
F1 02 HH LL   set EL Arduino position
```

The value is encoded in hundredths of a degree. WT4 immediately reads the axis
back and confirms it matches. A successful Arduino position write resets the
WT4 software calibration offset for that axis to zero so calibration is not
applied twice.

This does not write to the SVH3 quadrature pulse generator itself; that encoder
has no writable memory. It writes the position counter held by the Arduino
firmware.

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
to save it. Existing `wt4.ini` files that still contain `5.000` will keep that
value until changed.

## Safety Limits

Each antenna has independent limits. Press `Limits` in the GUI to edit and save
these values without hand-editing `wt4.ini`:

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

The default is 60 seconds. If a button-release event is missed, WT4 stops the
axis when this time expires.

The Limits dialog validates numeric ranges before saving. New limits take effect
immediately for connected antennas and are written to `wt4.ini`.

Elevation limits and elevation calibration values are constrained to 0..90
degrees throughout WT4.

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

Automatic slews are also limit-aware. If the configured azimuth range wraps
around 0 degrees, for example `az_min = 270` and `az_max = 265`, WT4 treats
265..270 as a forbidden dead-zone and chooses the slew direction that remains
inside the allowed arc, even when that is not the shortest geometric rotation.

Because there are no physical limit switches, loss of encoder replies or any
protocol error is treated as a fault and movement stops.

## Park

Press `Park` to slew each connected antenna to its configured park position,
stop motion, and disconnect from the controllers after both antennas have
parked successfully. If any antenna faults or `STOP ALL` is pressed during
parking, WT4 stops movement and stays connected so the fault remains visible.

Park positions are edited in the `Park` tab inside `Limits`:

```ini
park_az = 355.000
park_el = 80.000
```

Park EL must be 0..90 degrees and the park position must also be inside that
antenna's configured software limits.

## Target Tracking

WT4 includes guarded target tracking:

- `Track Sun` computes the current Sun AZ/EL and slews both connected antennas toward it.
- `Track Moon` computes the current topocentric Moon AZ/EL and slews both connected antennas toward it.
- `Track Source` tracks the selected RA/Dec source from the `Sources` dialog.
- `Stop Track` stops tracking and sends stop commands.
- Antenna status shows `SLEWING` during the initial gross move to a target and
  `TRACKING` once on target. Later fine tracking corrections stay labelled
  `TRACKING`.
- AZ and EL are allowed to slew concurrently on each antenna.
- Observer latitude/longitude are edited with `Observer`.
- Named RA/Dec radio sources are edited and selected with `Sources`.
- `Interval`, AZ/EL start tolerance, AZ/EL stop tolerance, AZ/EL slow speed, AZ/EL slow deg, AZ/EL tracking speed, and `Max jog` are edited with `Tracking`.
- The main screen shows one shared target AZ/EL; the OLED displays use the
  same shared target values.
- Sun and Moon AZ/EL are shown continuously as reference positions, even when
  they are not being tracked.

All tracking uses the same calibrated positions, software limits, margins, jog
speed, max-jog watchdog, encoder polling, and stop commands as manual movement.
If the target is outside the configured safe limits, WT4 stops instead of
moving.

Each antenna has separate AZ and EL tracking speeds:

```ini
az_track_speed = 40
el_track_speed = 40
```

Each axis also has its own tracking tolerance and slow-rate settings:

```ini
az_track_tolerance_degrees = 0.10
el_track_tolerance_degrees = 0.10
az_stop_tolerance_degrees = 0.10
el_stop_tolerance_degrees = 0.10
az_slow_speed = 20
el_slow_speed = 20
az_slow_threshold_degrees = 3.0
el_slow_threshold_degrees = 3.0
```

The track tolerance values are start tolerances: an axis does not move until its
error is larger than that axis' start tolerance. Once an axis has started moving,
it stops when it reaches that axis' signed stop tolerance. Positive stop
tolerance stops before/at the target band; negative stop tolerance intentionally
continues through the target and stops after passing it by that amount.

When an axis is within its slow-degree value, WT4 changes that axis to its slow
speed until it reaches that axis' stop tolerance.

Fine tracking moves that start already inside the slow-degree range begin at the
axis slow speed. The Tracking dialog requires each axis slow speed to be lower
than the matching antenna tracking speed.

`Interval` is limited to 0.1..10.0 seconds in 0.1 second steps. Each axis start
tolerance is limited to +/-0.01..0.20 degrees in 0.01 degree steps. A negative
start tolerance leads the target on that axis by that amount. Each stop
tolerance is limited to +/-0.01 degrees up to the absolute value of the matching
start tolerance.

Use low speed for the first tests and confirm the displayed target AZ/EL is
reasonable before allowing larger slews.

Moon tracking uses an internal lunar model with topocentric parallax correction,
so the Raspberry Pi does not need an internet connection or downloaded
ephemeris files. RA/Dec source tracking uses local sidereal time from the
observer longitude.

## OLED Display

WT4 writes the OLED over the decoded display command:

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

WT4 does not yet include full astronomical schedule tracking or automatic scan
patterns. Those should come after guarded target tracking is proven on both
antennas.
