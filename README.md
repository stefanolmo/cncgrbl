# CNC Deck

<p align="center"><img src="docs/splash.png" alt="CNC Deck - splash screen" width="640"></p>

G-code sender for **GRBL 1.1** CNCs (designed for the CNC 1610 Pro) with a dark interface and a 3D viewer, plus an **SVG → G-code** converter.
Python 3 + Qt6 (PySide6) + pyserial. Tested on Ubuntu 26.04 LTS.

## What it is

A single window with two modes, selected with the switch at the top left.

**CNC Sender** – load a `.gcode`, control the machine (DRO, jog, Z probe, overrides, spindle) and stream the program with a live 3D preview. Here the sample job is running on the built-in GRBL simulator.

<p align="center"><img src="docs/sender.png" alt="CNC Deck - CNC Sender mode" width="900"></p>

**SVG Converter** – load an `.svg`, create one or more operations (outside, inside, pocket, engrave, with holding tabs), preview the toolpath in 3D, then save the `.gcode` or press **Send to CNC** to load it straight into the Sender.

<p align="center"><img src="docs/converter.png" alt="CNC Deck - SVG Converter mode" width="900"></p>

## Installation (Ubuntu)

```bash
./install.sh          # venv in ~/.local/share/cncdeck, "cncdeck" launcher, menu entry
cncdeck               # or: cncdeck examples/demo.nc
cncdeck --sim         # try everything without a machine (built-in GRBL simulator)
cncdeck --no-splash   # skip the 3 second splash screen (or CNCDECK_NO_SPLASH=1)
```

Without installing: `pip install -r requirements.txt && python3 -m cncdeck`.

## Installation as a .deb package (Ubuntu/Debian)

A "distribution style" package: it contains only CNC Deck and uses the system Python libraries (`python3-pyside6.*`, `python3-numpy`,
`python3-serial`, `python3-pyclipper`); only `svgelements` is bundled. Install and remove it with apt or Synaptic:

```bash
sudo apt install ./packaging/deb/cncdeck_0.6.0_all.deb     # also installs the system dependencies
sudo apt remove cncdeck                                    # uninstall (or from Synaptic / Software)
```

- Requires the `python3-pyside6.*` and `python3-pyclipper` packages to exist in your Ubuntu release's repositories
  (check: `apt-cache policy python3-pyside6.qtwidgets python3-pyclipper`). If they are missing, use `./install.sh`.
- Serial port: `sudo usermod -aG dialout $USER` (then log out and back in); see also `/usr/share/doc/cncdeck/README.Debian`.
- If you installed with `install.sh`, remove that copy first (see README.Debian) so you do not end up with two versions.
- To rebuild the package: `packaging/build-deb.sh` (needs `dpkg-deb` and pip; writes to `dist/`).

**Serial port on Ubuntu** (the classic causes of "I cannot see the port"):
- your user must be in the `dialout` group: `sudo usermod -aG dialout $USER`, then log out and back in;
- `brltty` steals CH340 boards (the 1610 Pro uses one): `sudo apt remove brltty`.

## Features

| | |
|---|---|
| Streaming | *character-counting* protocol (127-byte RX buffer), no pauses between lines; pause = feed hold `!`, stop = hold + soft reset (the position stays valid) |
| 3D viewer | orbit (left), pan (right/middle), zoom (wheel), double-click = fit; colour by Z height, dashed rapids, already executed part in grey, live tool, extent, machine area ($130-132) |
| Program | editor with highlighting, running line, time estimate (also remaining, corrected for the override), warnings (G1 without F, lines > 80 characters, unsupported G-codes, travel exceeded) |
| Resume | right-click a line → *Start from line…* restores units/feed/spindle, rises, moves in XY and descends |
| DRO | work and machine coordinates, per-axis zeroing (`G10 L20 P0`, persistent), go to zero, safe Z |
| Jog | step 0.01–50 mm or continuous (stops at once on release with jog-cancel), optional keyboard (arrows, PgUp/PgDn) |
| Z probe | slow `G38.2` descent onto a touch plate, zeroes Z with the given thickness and retracts |
| Height map | automatic leveling: probes a grid over the G-code area and corrects the Z of every move before it is sent. See below |
| Override | feed, rapids, spindle (real-time commands) |
| Spindle | M3/M5 with adjustable S |
| SVG Converter | «SVG Converter» mode (switch at the top left): load an `.svg`, choose operations and tool, live 3D preview, **Save .gcode** or **Send to CNC ➜** (loads the G-code into the Sender without going through the disk). See below |
| Other | homing, unlock, reset, check mode, console with history, `$$` editor with descriptions, 6 custom macros, file drag & drop, M6/T converted to an M0 pause for manual tool changes |

## SVG → G-code converter

The [jscut](https://github.com/tbfleming/jscut) approach, rewritten in Python with `pyclipper` (Clipper bindings, the same
geometry library as jscut) and `svgelements` (transforms, units, viewBox):

- **Operations** (you start with an empty list, like jscut: create them with «＋ Add»; several are allowed, run in sequence): *Outside*, *Inside*, *Pocket*, *Engrave* (centre line, open paths too);
  for each one: shapes used, combination (union / intersection / difference / XOR), direction (conventional / climb, for a
  clockwise spindle), total depth and depth per pass, step over, band width, offset, ramp entry.
- **Tool**: diameter, cut and plunge feed rates, safe Z, optional M3/M5, G-code units (mm or inches), zero position.
- **Y convention like jscut**: Y_gcode = −Y_svg. With «Top-left corner of the design» (default) the zero sits at the top left of the
  part and the job extends into negative Y, exactly like the files you already use. Your work zero must coincide with that point.
- **Optimisations over jscut's output**: moves with `G0` (not `G1` with huge feeds), no duplicate or collinear points,
  feed written only when it changes, short lines, `G21 G90 G94` header, ends with `G0` to safe Z and `M2`. The generated G-code
  goes through the same parser and the same streaming as the Sender: a test sends it to the GRBL simulator without errors.
- The spindle is **not** switched on by default (like jscut): use the Spindle button in the Sender or enable M3/M5 in the converter.
- «Send to CNC» does **not** start the job: it moves the program into the Sender and switches to that mode; check the zero and press Run.

- **Holding tabs** (Outside/Inside only): along the cut the tool stays at `Height` mm from the bottom, so the part neither detaches
  nor moves. *Automatic*: N evenly spaced tabs on the contour of each part (holes get none). *From SVG shapes*: draw a
  small circle/rectangle straddling the edge and mark it as a tab; the shape is not milled and covers the path up to one
  tool radius away from it. They need a single side pass (width = tool diameter) and a height smaller than the depth.

Not implemented compared to jscut: *V-pocket*, Z origin from the bottom of the material, SVG text conversion (convert to paths).

The viewer assumes absolute-coordinate programs (G90) starting from the work zero; a G91-only program is drawn starting from 0,0,0.

## Height map (leveling with the probe)

Sender only. GRBL 1.1 does not compensate for flatness, so CNC Deck does it: it measures the surface and corrects the G-code before sending it
(the original file is not modified).

1. Load the G-code. Switch the spindle off, connect the probe clip to the tool and to the material (copper/metal), zero X, Y and Z
   **on the surface at the job origin** and raise the tool. If the material is not conductive, put a plate on it and enter its thickness.
2. **Height map** card → *Probe the job area*: the grid covers the extent of the cutting moves only, plus the margin, with a step ≤ the chosen value.
   For each point: rise to safe Z, move, descend quickly down to *Approach* above the last contact, then probe slowly (`G38.2`).
   The first descent starts from safe Z. *Cancel* (or Stop) aborts; no contact within *Max depth*, an alarm or an error stops everything.
3. The map is shown in the viewer (exaggerated relief, colours by height). With «Apply the map when I run the program» ticked, *Run* and *Start from line…* send
   the corrected G-code: every cut is split at each grid line and the surface height is added at each point (bilinear interpolation).
   Arcs become `G1` segments; rapids only correct the end height. The map can be saved/loaded (`.json`).

Limits and cautions:
- The map is in **machine** coordinates: it stays valid if you change the work zero, but **not** if you move the workpiece or the bed.
- Programs with `G92`, `G10`, `G55`–`G59` are refused (they would change the coordinate system); after `G28/G30/G53` absolute moves are required.
- Outside the mapped area the edge height is used (you are warned before starting).
- Accuracy: that of your probe and of GRBL (typically 0.01–0.03 mm at 25 mm/min). Of little use for deep milling; fine for engraving/PCBs.
- Time: a 10 mm grid over 150×90 mm ≈ 150 points, 10–15 minutes. The probe confirmation dialog shows the estimate.
- Tested only on the simulator (tilted plane): **the first time, try it on scrap, with the spindle off.**

## Development

```bash
pip install -e .[dev]
python -m pytest            # tests: parser, protocol/streaming against the simulator, SVG/CAM conversion, offscreen UI
python examples/make_demo.py
QT_QPA_PLATFORM=offscreen python3 docs/make_screenshots.py   # regenerate the README images
```

`cncdeck/simulator.py` emulates GRBL 1.1 (127-byte RX buffer, 15-block planner, hold/jog-cancel, WCO, probe,
overrides) and is used both by the `--sim` mode and by the tests: the tests check that streaming never exceeds
the RX buffer.

**Important**: the tests run on the simulator; behaviour on the real board must be checked with care
(first a dry run, or *Check mode*, without a tool and with a hand on the emergency stop).
