# Prompt: rebuild "CNC Deck" (GRBL sender + SVG → G-code converter)

> To be pasted in full into another programming AI (with shell and filesystem access). It describes the software
> **as it is at version 0.6.0**: purpose, stack, architecture, exact behaviours, conventions not to change,
> traps already hit and acceptance criteria. The sections «Binding decisions» and «Known traps» contain lessons
> paid for with real bugs: read them before writing code.

---

## 0. How you must work

1. You write all the code yourself, test everything, and do not declare "done" without having run the tests and **looked at** the
   screenshots of the interface (see §13). Show the test output; if something fails, say so.
2. Proceed module by module, in the order of §3, and after each one write/run its tests. Do not move on with red tests.
3. You have no CNC machine: use the **GRBL simulator** described in §6 (you write it) for every streaming test.
4. User interface and messages in **English**. Code, names and comments in English. Comment only where explaining a non-obvious "why".
5. Do not add features that were not requested. Where a choice is unspecified, pick the simplest and document it in the README.
6. At the end: README, `install.sh`, `.desktop`, icon, `pyproject.toml`, tests, README screenshots (§14).

## 1. Product

**CNC Deck** is a Linux desktop application (tested on **Ubuntu 26.04 LTS**) that:

- **Mode 1 – CNC Sender**: loads a G-code file, previews it in 3D and streams it over a serial port to a CNC with
  **GRBL 1.1** (reference machine: **CNC 1610 Pro**, CH340 board, 115200 baud, ~160×100×45 mm area, PWM spindle). It offers the
  Candle features (DRO, jog, zero, probe, overrides, `$$` settings, console, height-map leveling) with nicer graphics.
- **Mode 2 – SVG Converter**: loads an `.svg`, the user configures tool and operations (**jscut** approach), sees a 3D preview of
  the toolpath, and can **save** the `.gcode` or **send it to the Sender in memory** with one click ("Send to CNC ➜"), without going
  through the disk. Sending only loads: it **never starts** the cut by itself.
- A single main window; a two-position switch in the toolbar ("CNC Sender" | "SVG Converter") changes page instantly. The serial
  connection and a running job stay active while the converter is shown.
- Animated 3 second splash screen.
- **Machine-less** demo mode: `cncdeck --sim` connects a built-in GRBL simulator.

Typical user: hobbyist, machine with a moving bed on the Y axis (never say so in the interface), G-code files coming from jscut and
similar with **negative Y** (zero at the top left of the design), air moves made with `G1` instead of `G0`.

## 2. Stack and constraints

- Python ≥ 3.10 (developed on 3.13). **PySide6** (Qt 6, `PySide6-Essentials`), **pyserial**, **numpy**, **pyclipper** (Clipper
  bindings, the same geometry library as jscut), **svgelements** (SVG parsing). Tests: **pytest**.
- No OpenGL: the 3D viewer uses only `QPainter` + numpy (robust on any driver).
- No external assets: icons drawn in code with `QPainter`; theme via Qt stylesheet (QSS). No image files in the package apart from the
  desktop entry SVG icon in `packaging/`.
- `cncdeck/` package with entry point `cncdeck = cncdeck.__main__:main` (`pyproject.toml`, setuptools). Version in
  `cncdeck/__init__.py` (`__version__`) and in `pyproject.toml`, always equal; the version appears in the window title
  ("CNC Deck 0.6.0") and in the splash.
- Headless: all tests run with `QT_QPA_PLATFORM=offscreen` (set in `tests/conftest.py`).
- On Ubuntu the system packages `libxcb-cursor0 libxkbcommon-x11-0 libegl1 libgl1 libdbus-1-3 libfontconfig1` are needed.

## 3. Repository layout (suggested build order)

```
cncdeck/
  __init__.py          __version__
  gcode.py             G-code parser → toolpath, times, warnings, resume from a line         (§4)
  grbl.py              GRBL protocol, serial thread, streaming                              (§5)
  simulator.py         GRBL emulator with a pyserial-like API                                (§6)
  theme.py             palette, QSS, vector icons                                            (§7)
  viewer.py            QPainter 3D viewer                                                    (§8)
  widgets.py           Card, StateBadge, AxisRow, JogPad, OverrideBox, CodeEditor, Console   (§9)
  settings_dialog.py   GRBL `$` settings editor                                              (§10)
  heightmap.py         height map + G-code correction                                       (§10.1)
  probing.py           automatic probing grid                                                (§10.1)
  mainwindow.py        main window, Sender mode                                              (§10)
  svgimport.py         SVG → polylines in mm                                                 (§11)
  cam.py               jscut operations + G-code generation + tabs                           (§12)
  convertpage.py       "SVG Converter" page                                                  (§12)
  splash.py            splash screen                                                         (§12)
  __main__.py          argparse, start-up, splash
tests/                 test_gcode, test_controller, test_viewer, test_svg_cam, test_heightmap, test_ui   (§13)
examples/              demo.nc (+ make_demo.py), demo.svg, ellipse_cam.gcode (real jscut file, negative Y)
docs/                  README images + make_screenshots.py
packaging/             cncdeck.svg, cncdeck.desktop, cncdeck.1, build-deb.sh
install.sh  README.md  pyproject.toml  requirements.txt  .gitignore
```

## 4. `gcode.py` — parsing

**`clean_line(raw)`**: removes `(...)` and `;...` comments and all spaces, upper-cases, strips a leading `/`; a line starting with
`%` becomes empty; strips a leading `N\d+` line number; if the line does **not** start with `$`: removes `T\d+` words and
turns `M6`/`M06` (not `M60`…) into `M0` (GRBL 1.1 rejects T/M6 with `error:20`; M0 = pause for a manual tool change).

**`parse_program(text, rapid_rates=(500,500,500), default_feed=0) -> Program`**

- `Program`: `source` (original lines), `commands` = list of `(source_line_index, cleaned_line)` for the non-empty lines only,
  `path` (`Toolpath`), `bounds_min/max` (3-tuples), `feed_length`, `rapid_length`, `line_time` (s per source line), `cum_time`
  (cumulative, length n+1), `max_spindle`, `warnings`; members `total_time`, `size`, `time_remaining(line)`.
- `Toolpath`: numpy arrays `start`, `end` (M×3 float32, **mm**), `kind` (uint8: `RAPID=0`, `FEED=1`), `line` (int32, source line;
  non-decreasing).
- Modal state: motion G0/G1/G2/G3 (G38.2–38.5 also as feed moves), G4 (dwell `P` seconds → time), planes G17/18/19,
  units G20/G21 (everything converted to mm; feed in units/min × 25.4 if inches), G90/G91, `F`, `S` (maximum → `max_spindle`).
  G53/G28/G30/G10/G92/G54–59/G93/G94/G90.1/G91.1/G40/G49/G43.1/G80/G61 produce no motion in the drawing and no warning.
  Other G codes → warning "Line N: G41 not supported by GRBL 1.1" (once per code).
- Start position **assumed (0,0,0)** (the real one is unknown).
- **Arcs** G2/G3 with `I J K` (centre format) or `R`. Plane mapping as GRBL: G17 axes (X,Y) linear Z offsets (I,J); G18 axes
  (Z,X) linear Y offsets (K,I); G19 axes (Y,Z) linear X offsets (J,K). R format with GRBL's formula
  (`h_x2_div_d = -sqrt(4r²-x²-y²)/hypot(x,y)`, signs for CCW and negative R; shared helper `arc_center_from_radius`). Angle: CW if
  `travel >= -eps` → `-2π`, CCW if `travel <= eps` → `+2π`; start=end with IJ ⇒ full circle. Linear interpolation of the free axis
  (helix). Subdivision with 0.02 mm tolerance (`step = 2·acos(1 - tol/r)`, min 2, max 2000 segments), the last point lands exactly on the target.
- **Classification for the drawing**: G0 → RAPID. G1 → FEED, **except** (a) the **first** real move of the program (it starts from an
  unknown position) and (b) G1 moves with **both** ends at Z > 0 (jscut-style CAM positions with G1 above the work): these are RAPID
  **for the drawing only**; time and lengths stay those of the command (`motion == 0` ⇒ rapid).
- **Bounds**: computed **only on FEED segments** (extent of the part, not of the air moves); if there are none, on all of them.
- **Times**: rapids `max_i(|d_i| / rate_i) · 60` with the maximum rates `$110–112` (default 500); feeds `dist/feed·60`;
  dwell added. No acceleration (estimate).
- **Warnings** (English text, prefix "Line N: "): G1/arc without F; line > 79 characters after cleaning (GRBL limit 80);
  unsupported G; invalid arc radius; **radius almost equal to half the chord** (`4r²-d² < 0.01`: GRBL may reject it with
  `error:34`); M6 converted to an M0 pause.
- **`preamble_for(program, line)`**: replays the modal state up to `line` and returns the commands to resume safely:
  `G21|G20`, `G{plane}`, `G90`, `M3|M4 S..` if the spindle was on, `G1F{feed}`, `G0Z{safe}` (safe = max Z seen + 5, or 5),
  `G0X..Y..`, `G1Z..`, and `G91` if it was relative.
- `format_duration(s)` → `mm:ss` or `h:mm:ss`.

## 5. `grbl.py` — protocol and serial thread

**Constants**: `RX_BUFFER = 127`; `SIM_PORT = "simulator"`; real-time bytes: `?` `!` `~` `0x18` (reset), `0x85` (jog cancel),
feed override `0x90`(100%) `0x91`(+10) `0x92`(−10) `0x93`(+1) `0x94`(−1), rapids `0x95/0x96/0x97` (100/50/25%), spindle
`0x99`(100%) `0x9A/9B/9C/9D` (+10/−10/+1/−1), `0x9E` spindle stop toggle. `ERRORS` (1–38) and `ALARMS` (1–9) tables with
**English** texts; `SETTING_INFO` ($0–$132: English description + unit).

**`Status`** (dataclass): `state` ("Idle", "Run", "Hold:0", …; "Disconnected"/"Connecting" on the UI side), `mpos`, `wpos`, `wco`,
`feed`, `spindle`, `ov_feed/ov_rapid/ov_spindle`, `pins`, `planner_free`, `rx_free`, `accessories`, property `base_state`
(before the `:`). **`parse_status(line, prev)`** parses `<Idle|MPos:..|WPos:..|WCO:..|FS:..|F:..|Ov:..|Pn:..|Bf:..|A:..>`
carrying over absent fields; if MPos arrives it computes `wpos = mpos − wco`, if WPos arrives it computes `mpos = wpos + wco`.

**`GrblConnection(QThread)`** — owns the port; all public methods are thread-safe (lock + queues):
- Signals: `connectionChanged(bool,str)`, `statusChanged(object)`, `console(str,str kind)` (kind: tx rx info error alarm msg),
  `lineAcked(int tag,bool ok,int code)`, `programState(str)` (running paused done aborted error), `settingsChanged(object)`
  (**`object`, not `dict`**: a `Signal(dict)` with int keys is not convertible to C++), `parserState`, `probeResult(x,y,z,ok)`,
  `alarmRaised(int,str)`, `versionInfo`.
- API: `send_line(text, silent=False)`, `send_realtime(bytes)`, `start_program(commands)`, `pause()`, `resume()`, `stop()`,
  `reset()`, `disconnect_port()`, properties `settings`, `status`, `is_streaming`.
- **Opening**: `serial.Serial(port, baud, timeout=0.005, write_timeout=1.0)`; for `SIM_PORT` it uses `SimulatedSerial`.
  **Handshake**: Arduinos reset when the port opens → wait for the `Grbl ...` banner; at 0.8 s send `?` (the board may already be
  alive), at 2 s send reset `0x18`, at 5 s error "No response from GRBL (wrong baud rate or port?)". Then silently queue `$I`, `$$`, `$G`.
  Error messages: "Cannot open PORT: …", "Connection lost: …".
- **Character-counting streaming**: `rx_used = sum of bytes (line+"\n") of the commands awaiting an ack`; a line is sent only if
  `rx_used + n <= 127`. **Manual** lines (console, jog, zero…) take priority over program lines. Tags: `>=0` program index,
  `-1` manual (echoed in the console), `-2` internal silent. Real-time bytes (`?`, `!`…) do not count in the buffer.
- **Status polling**: `?` every 0.2 s (0.1 s in Run/Jog/Home).
- **ok / error:N**: pops the oldest pending command. On an error of a **program** line: sets `_error_hold`, sends `!` (feed hold),
  emits `programState("error")`; the program resumes only with `resume()`. An `ok/error` with no pending command is normal within
  1.5 s of a reset (do not emit the "sync lost" error in that case).
- **End of program**: all lines acknowledged **and** state `Idle` **and** no pending command ⇒ `programState("done")`.
- **pause()**: `!` and stop sending program lines. **resume()**: `~`, clears `_paused`/`_error_hold`.
- **stop()**: clears queues, sends `!`, and when the state becomes `Hold:0` (or Idle/Alarm, or after 4 s) sends the **soft reset** `0x18`
  (the position stays valid: a reset from Hold does not raise an alarm); emits `aborted`. **reset()**: immediate reset.
- **Banner** `Grbl ...` (after a reset): clears the pending queue, aborts a running program (`aborted`).
- **Settings**: `$N=v` lines → cache; the `ok` of `$$` emits `settingsChanged`; after each successful `$N=v` write it re-issues `$$`.
  `[PRB:x,y,z:1]` → `probeResult`; `ALARM:n` → `alarmRaised` + `aborted`; `[GC:..]` → `parserState`.
- Port errors (`SerialException`/`OSError`) → `connectionChanged(False, "Connection lost…")` and a clean close.
- On a clean close, if in Run/Jog, it sends `!` before closing the port.

## 6. `simulator.py` — GRBL 1.1 emulator

`SimulatedSerial(speed=6.0, boot_delay=0.2)` with a pyserial-like API (`write`, `read(size)` with timeout, `in_waiting`, `close`,
`timeout`, `is_open`) and an internal thread. It must model what a sender needs:
- 127-byte RX buffer (counters `overflows` and `max_rx_used`: the tests check that streaming **never exceeds it**);
  a byte is freed when the line enters the planner; **15-block** planner: `ok` only when the line is accepted;
- banner `Grbl 1.1h ['$' for help]` after `boot_delay`; status report `<State|MPos:..|Bf:..|FS:..>` (with `WCO:` every 10 reports,
  `Ov:` every 5; `Pn:P` while the tip is in contact with the virtual surface); states Idle/Run/Jog/Hold:0/Alarm/Home/Check;
- `!` `~` `0x18` (from motion → ALARM:3) `0x85` (cancels the jog) and all overrides (`0x90–0x9E`);
- commands: G0–G3 (arcs treated as motion to the target), G4, G20/21, G90/91, G54–59, G10 L20, G92, G38.2 (virtual contact at machine
  Z −18 → `[PRB:x,y,z:1]`, otherwise ALARM:5; an optional `probe_surface(x, y)` callable gives a tilted/warped test surface),
  M0 (pause: goes to `Hold:0` until `~` arrives), M3/M4/M5, `$J=` (error 8 if not Idle, 22 without F), `$$` (typical 1610 Pro defaults:
  `$110=2000 $111=2000 $112=800 $130=160 $131=100 $132=45 $30=1000 $20=0 $22=0 …`), `$G`, `$I`, `$X`, `$H`, `$C`, `$N=v`;
- errors: `error:22` (G1/G2/G3 without F), `error:20` (unsupported G), `error:34` (arc with radius ≤ half the chord), `error:3`,
  `error:9` in alarm;
- `speed` = time acceleration factor of the simulated time (tests raise or lower it depending on whether they must observe states).

## 7. `theme.py` — look

Palette (hex): `BG #0e1217`, `PANEL #141a21`, `CARD #1a222c`, `CARD_HI #222c38`, `BORDER #2a3544`, `TEXT #d9e1ea`,
`MUTED #7f8d9f`, `ACCENT #2dd4bf`, `ACCENT_DIM #14766b`, `OK #34d399`, `RUN #4aa8ff`, `WARN #fbbf24`, `DANGER #f87171`,
axes `X #f87171`, `Y #4ade80`, `Z #60a5fa`. Font: system; monospace `JetBrains Mono, Ubuntu Mono, DejaVu Sans Mono,
Liberation Mono, monospace`. Dark theme with rounded borders (card 10 px, buttons 7 px), `#primary` buttons (solid accent),
`#danger`, `QToolButton#run` (green) and `#stopb` (red) in the toolbar, `QToolButton#jog`, styles for QComboBox/QSpinBox/
QLineEdit/QSlider/QTabBar/QScrollBar/QProgressBar/QStatusBar/QToolBar/QMenu/**QListWidget** (selection `ACCENT_DIM`,
themed check indicator).
**QSS trap**: do not use a broad descendant selector (`QFrame#card QWidget {background: transparent}`): by specificity it
beats `QPushButton#primary` and makes the buttons transparent. Make only `QLabel/QCheckBox/QSlider` transparent inside cards.
`STATE_COLORS` for the status badge. Vector icons with `QPainter` (`icon(name, color)`; grey disabled version):
play pause stop open home unlock reset gear probe check up down left right target spindle save reload skip cube fit hold.

## 8. `viewer.py` — `ToolpathView(QWidget)` (QPainter + numpy, orthographic projection)

**Axis convention (BINDING, see §15)**: **right-handed** view: Z up on screen; top view with **X to the right and Y up**. Matrix:
`q = Rx(pitch) · Rz(yaw) · (p − centre)` with
`Rz = [[cz,−sz,0],[sz,cz,0],[0,0,1]]` and `Rx = [[1,0,0],[0,cx,sx],[0,−sx,cx]]` (the sign of `sx` is essential: with the opposite Z goes
down). Screen: `x = W/2 + pan_x + qx·scale`, `y = H/2 + pan_y − qy·scale`.
- Views: `iso (yaw −35, pitch 55)`, `top (0,0)`, `front (0,90)`, `right (−90,90)`, `left (90,90)`.
- **"Grab" mouse orbit** (`orbit(dx,dy)`): `yaw += dx·0.4`, `pitch = clamp(pitch − dy·0.4, 0, 180)`: dragging right makes the
  surface follow the cursor to the right; dragging down lowers the near edge. Right/middle button = pan; wheel = zoom towards
  the cursor (`1.0015**delta`); double-click = fit all.
- Drawing: gradient background, grid on the Z=0 plane (adaptive step, labelled axes), coloured XYZ axes at the origin, dashed bounding
  box with dimensions, optional machine area, **guides** (`set_guides`: faint (N,3) polylines, e.g. the SVG design), **height map**
  (`set_heightmap(hm_in_work_coordinates)`: relief mesh with exaggerated height, colours by height, label "map NxM Δ x mm (relief ×k)"),
  path: **FEED coloured by Z** (24 buckets, orange→yellow→green→cyan ramp; legend on the right), faint **dashed RAPIDs**
  (can be hidden), **already executed part in grey** (`set_progress_line(line)` → index via `searchsorted` on `path.line`),
  **tool marker** = white shank + cyan tip with halo, dashed shadow towards Z=0.
- Performance: cached layer (`QImage`) recomputed only when the camera changes; during interaction it decimates segments
  (max ~25,000); incremental "executed" layer. `set_program(prog, fit=True)`; with no program and some guides, the scene bounds are
  those of the guides; empty scene: `lo=(−10,−50,−5) hi=(50,10,10)` (room towards −Y where negative-Y parts sit).
- Customisable `empty_message` (text in the middle when there is no path). HUD at the bottom: `X a…b Y c…d Z e…f mm`.
- The **marker follows `WPos` exactly** in the same coordinates as the path: no inversion.
- `add_view_buttons(view)` (end of the module): floating bar 3D/Top/Front/Side/Fit/Rapids for the converter page.

## 9. `widgets.py`

`Card(title)` (`#card` QFrame with an upper-case 11 px spaced title); `StateBadge` (coloured pill per state, labels:
Not connected/Connecting…/Ready/Running/Paused/Jog/ALARM/Homing/Check mode/Door open/Sleep); `AxisRow(axis)`:
coloured label, **large monospace work position**, small machine position ("M x.xxx"), `X0` button (zero) and
crosshair button (go to 0, tooltip "Go to X=0"); `JogPad`: XY cross (↑ ← → ↓ 52 px) + Z column; signals `pressed(dx,dy,dz)` and `released`; **up arrow =
(0,+1,0) = Y+, down arrow = Y−**, tooltips "Y+ (↑)" / "Y− (↓)", Z "PgUp/PgDn"; `OverrideBox`/`RapidBox` (−10 −1 100 +1 +10 / 25 50 100);
`CodeEditor(QPlainTextEdit)` with line numbers in the margin, G-code syntax highlighting (G purple, M yellow, X/Y/Z in the axis colours,
IJKR light blue, F orange, S green, comments grey italic, `$` yellow), highlighted running line (`set_exec_line`),
lines with warnings in red, context menu "Start from line N…"; `Console` (HTML log coloured by kind, max 3000 lines, command field
with ↑↓ history, Clear button).

## 10. `mainwindow.py` — Sender mode

**Structure**: central `QStackedWidget` (page 0 = Sender, 1 = `ConverterPage`). Toolbar: on the left the two-segment switch
"CNC Sender | SVG Converter"; then (visible **only** in Sender mode): Open, Reload, Save | Run, Pause/Resume,
Stop | Home, Unlock, Reset, Check, Probe Z; at the end always visible: GRBL (settings). Status bar: state/port/pins, buffer
(`Buffer: planner N rx N`), time (`elapsed / remaining ≈`), 0–1000 progress bar.

**Sender page**: on the left a scrolling column (410–440 px) with cards: **Connection** (port combo + baud
[115200 default, 57600, 38400, 19200, 9600, 250000], Connect/Disconnect button, state badge; port list from
`serial.tools.list_ports`, `ttyUSB*/ttyACM*` first, last entry "GRBL simulator (demo)"; automatic refresh every 3 s when not connected);
**Position** (3 `AxisRow`, "Zero all" = `G10L20P0X0Y0Z0`, "Go to XY 0" = `G90G0X0Y0`, "Safe Z" = `G90G0Z{safeZ}`, a row
"Feed … Spindle …"); **Jog** (pad, step [0.01, 0.1, 1 (default), 5, 10, 50 mm, Continuous], speed spin default 800 mm/min
(max = `$110/$111`), checkbox "Keyboard: arrows XY, PgUp/PgDn Z" (Sender mode only, ignored if focus is on text fields),
"Safe Z" spin default 5 mm); **Spindle** (toggle `M3 S..`/`M5`, slider+spin S up to `$30`); **Height map** (§10.1); **Macros** (6 buttons,
right-click to set name and commands, saved in QSettings, several commands separated by `;`); **Override** (feed,
rapids, spindle). On the right: 3D viewer with the view bar + red **alarm** banner (Unlock `$X` / Homing `$H`) + amber **GRBL
error** banner + **Program** tab (summary line: name, number of commands, size, cutting mm, estimated time, number of warnings; editor) and
**Console**.

**Behaviours**:
- **Jog**: step → `$J=G91G21{axes}F{feed}` (silent); for Z the feed is limited to `$112`. **Continuous**: distance = maximum travel
  of the axis (`$130–132`, fallback 150/45 mm) and on release `0x85`; if `$20` (soft limits) is on it uses `$J=G53G21G90{axis}{target}`
  with a target inside the limit (−1 mm from the edge, machine space [−travel, 0]). Never with a program running or a state other than Idle/Jog.
- **Zero**: `G10L20P0{axis}0` (persistent in the current coordinate system). **Probe Z**: dialog for the plate thickness (default 15 mm,
  remembered) + confirmation, then `G91G38.2Z-30F25` and `G90`; on `[PRB:..:1]`: `G10L20P0Z{thickness}`, `G91G0Z{max(5,safeZ)}`, `G90`.
- **Run**: enabled only when connected, Idle/Check, program loaded and not probing. If there are warnings it asks for confirmation. **Start from
  line**: `preamble_for` + commands from the chosen line. **Pause**: `!`; **Resume** sends `~` (also if GRBL is in Hold from an M0 in the
  program, not a second `!`). **Stop**: hold + reset (also aborts probing). Program running: editor read-only, jog/zero disabled.
- **GRBL errors during the program**: **non-modal bar** (never blocking `QMessageBox` in the slot: one per rejected line stack up and
  freeze the window and its closing) with the **first** rejected line ("GRBL rejected line N: CMD → error:34 …
  Program paused." + "(+K more lines rejected, see Console)") and Resume / Abort buttons.
- **Progress on the drawing**: GRBL sends `ok` when the line enters the planner (up to ~15 blocks ahead): the "running" line is
  derived by snapping the real position (`wpos`) to the **nearest segment** of the path among the last ~30 lines sent (2 mm threshold,
  the last one on ties); UI update every 80 ms; remaining time = `time_remaining(line) · 100 / feed override`.
- The file loads from a dialog, drag & drop or `load_file`; **`load_gcode_text(text, name)`** loads from memory (used by the
  converter): `file_path=None`, title "name — CNC Deck x.y.z", refused if a program is running. Parsing happens in a
  thread (`ParseThread`); editor changes re-parse after 700 ms. Warnings in the console; a warning if the program exceeds the `$130–132` travel.
- Settings saved with `QSettings("cncdeck","cncdeck")` (geometry, port, baud, jog, safe Z, S, macros, height-map parameters, last folders…).
- `closeEvent`: if a program is running it asks for confirmation; saves the settings; removes the application event filter;
  `disconnect_port()` + `wait(3000)` and, if the thread does not end, `terminate()` (the app must **always** be able to exit).

**`settings_dialog.py`**: table `$ | Description | Value | Unit` (only the value is editable), "Reload", "Write changes"
(sends only the changed values, with confirmation). A table reload (late reply to `$$`) **keeps the cells already edited**.

### 10.1 Height map (auto-leveling, Sender only)

- **`heightmap.py`**: `HeightMap(xs, ys, z, created, note)` with axes and surface Z in **machine** coordinates (NaN = not probed yet);
  `blank`, `complete`, `spread`, `shifted(wco)` (work = machine − WCO), `z_at(x, y)` bilinear with clamping to the edges, `covers`,
  JSON `to_json/from_json` (`HeightMapError` on invalid files). `grid_axes(lo, hi, step)` (≥ 2 points, spacing ≤ step) and `serpentine(nx, ny)`.
  **`correct_program(commands, hm_in_work_coordinates, start)`** adds the surface height to the Z of every move: G1 split at each grid line
  crossed (XY projection), arcs (G17 only) linearised with the parser's helper into G1 segments, G0 corrects only its end point (and adds Z
  if the target Z ≤ 0.5), every emitted move has an explicit G word and absolute coordinates (distance mode restored if the program was
  relative), inches converted back, `F`/`S` kept on the first piece, `M` words on the last. Refuses (`HeightMapError`) `G92`/`G10`/`G55–59`,
  arcs on G18/G19, an incomplete map, an unknown start position (after G28/G30/G53 or without `start`).
- **`probing.py`**: `ProbeParams(area, step, safe_z, depth, clearance, feed, thickness)` with `validate()`; `HeightMapProbe(conn)` drives GRBL
  point by point (serpentine): `G0 Z{safe}`, `G0 XY`, `G0 Z(last contact + clearance)` (not for the first point), `G38.2 Z-{depth} F{feed}`.
  Refuses to start if the machine is not Idle, a program is streaming, or `Pn:P` is already closed. Signals `progress(done,total)`,
  `finished(HeightMap)`, `failed(str)`. Fails on: no contact (`ok=0`), alarm, GRBL `error:`, lost connection; `abort()` = `conn.stop()`.
  A `[PRB]` that is not at the expected XY is ignored (manual probe). The stored surface is `contact Z − thickness` (machine coords).
- **UI**: "Height map (leveling)" card: step, margin, max depth, speed, approach, plate thickness (saved), "Probe the job area" (area = FEED bounds of the
  program ± margin; confirmation dialog with points, estimated time and the safety checklist), Cancel, progress bar, label, checkbox "Apply the map when I
  run the program", Save…/Load…/Delete. `_leveled(cmds)` is applied in `run_program` and `run_from_line` (warns if the job exceeds the mapped area; shows
  the error and does not start on `HeightMapError`). A loaded map starts **inactive**. The map is shown in the viewer, shifted by the current WCO.

## 11. `svgimport.py`

`parse_svg(text, dpi=96.0, scale=1.0, tolerance=0.02) -> SvgDoc` with `svgelements.SVG.parse(reify=True, ppi=dpi)`: handles
nested transforms, viewBox, units, `<use>`. Every shape (rect, circle, ellipse, line, polyline, polygon, path; arcs
converted to cubics) becomes `SvgShape(sid, label, kind, subpaths[Subpath(points Nx2 mm, closed)], fill_rule)`; sid = `id` or
`kind+N`, unique. Coordinates in **mm with Y down** (origin = SVG page corner). Curves sampled at a step ≤ 0.25 mm
(min 8 points per curved segment) and then simplified with **Douglas-Peucker** (tolerance 0.02 mm, closed rings keep the first
point). `display:none`/`visibility:hidden` elements skipped; `<text>` ignored with a warning ("convert it to paths"); invalid
SVG → warning and empty list. `SvgDoc.bounds(ids)`.
**Performance trap**: svgelements' `seg.length()` is a very slow numerical integration (~20 s per file): estimate the
length by sampling 16 points.

## 12. Converter: `cam.py`, `convertpage.py`, `splash.py`

### 12.1 CAM (jscut approach with pyclipper)
Integer Clipper coordinates with `SCALE = 10000` units/mm; offsets with `JT_ROUND`, `ArcTolerance 0.005 mm`, miter 2.
- `shape_polygons(shape)`: closed subpaths combined with the shape's fill rule (`evenodd`/`nonzero`).
- `combine(polygons_per_shape, mode)`: union (default), intersection, difference (the first minus the others), XOR.
- **outline(geometry, tool, inside?, width, step_over, climb)** (port of `Cam.outline`): offset ±tool/2; `bounds` = band
  between the offsets (empty when width = tool); concentric rings at a step of `tool·step_over`; then `merge_paths`.
- **pocket**: inward offset tool/2, then steps of `−tool·step_over` until empty, innermost ring first.
- **engrave**: the centre line of the shapes, also open; closed ones: the first point is repeated.
- **merge_paths(bounds, rings)**: nearest neighbour; two rings are joined with a straight link **only if the whole
  segment lies inside `bounds`** (sampled every 0.2 mm with `PointInPolygon`, even/odd). **With empty `bounds` never merge**
  (do not replace `bounds` with the interior of the part: it would generate traversals **through the material**, a bug that really happened).
- **Direction**: with a clockwise spindle (M3) "conventional" = **outside clockwise, inside/pocket counter-clockwise**, "climb" the opposite.
  Since Clipper works in SVG orientation (Y down) and the output is mirrored, the code **inverts** the `climb` flag: a
  parametric test checks the sign of the path's area.
- `Operation`: `name, kind(inside|outside|pocket|engrave), shapes(None=all), combine, climb, cut_depth, pass_depth, step_over(0.4),
  width(0=tool), margin, ramp, tab_mode(none|auto|shapes), tab_count(4), tab_width(4), tab_height(1), tab_shapes`. Default name "Operation".
  `Job`: `tool_diameter 3.175, cut_feed 500, plunge_feed 150, safe_z 5, approach_z 1, spindle False, spindle_rpm 1000,
  units mm|in, origin top-left|bottom-left|svg, tolerance 0.01`. Defaults chosen for the 1610 Pro (depth 1 mm, pass 0.5 mm).
- Handling of empty cases: an empty input to Clipper (`Execute` with an empty subject) raises an exception → explicit guard.
  Tool too large → warning "NAME: the X mm tool is too large for this shape"; open path in Outside/Inside/Pocket
  → ignored with a warning (use Engrave); no shape selected → warning.

### 12.2 G-code generation (exact format)
**jscut** convention: `Y_gcode = −Y_svg`. Origin: `top-left` (default: top-left corner of the design → part in **negative Y**,
below the origin, like the real files), `bottom-left` (part in positive Y), `svg` (page origin). Z=0 = material surface,
cuts at negative Z. Output units mm (`G21`) or inches (`G20`, everything divided by 25.4). Numbers with at most 4 decimals, no trailing zeros.

```
; CNC Deck - SVG -> G-code conversion (jscut approach) - demo.svg
; Tool 3.175 mm, cut feed 500, plunge 150 mm/min
G21
G90
G94
G0 Z5                       (+ "M3 S1000" only if spindle=True)
; Operation 1: Cut out part (outside), depth 3, pass 1.5
; Path 0 [(with tabs)]
G0 X72.2477 Y1.5837         rapid move in XY at safe Z
G0 Z1                       rapid descent to the approach height (if < safe Z)
G1 Z-1.5 F150               plunge at the plunge feed
G1 X73.376 Y1.4857 F500     cut (F only when it changes: G94 is modal)
G1 X74.0043 Y1.375
...
G0 Z5                       rise at the end of the path
M2                          (preceded by "M5" if spindle=True)
```
Rules: **rapids always `G0`** (never G1 with huge feeds), no duplicate (<1e-4 mm) or collinear points (DP 0.01 mm), short lines
(<40 characters), no G2/G3 (polylines only), no M6/T/`N`. Passes: depth divided by `pass_depth`
(`-0.4,-0.8,-1` etc.). Closed paths: each pass makes one lap; **open** ones: back and forth alternately without lifting.
**Ramp** (jscut): in each pass Z descends linearly along one lap; at final depth one flat lap (closed) or flat return (open).
**If no path is generated the G-code is the empty string** (never a program with only the header); `CamResult` has
`gcode`, `guides` (design outlines in G-code coordinates, Z=0), `warnings`, `n_paths`. A single internal function `emit_pass`
produces all passes (with/without ramp, with/without tabs): the output without tabs must stay unchanged.

### 12.3 Holding tabs (Outside/Inside only)
Along the cut, on the passes deeper than `bottom + tab_height`, the tool stays at `tab_z = −cut_depth + tab_height`.
`auto`: N tabs of width W evenly spaced (centres at `(k+0.5)·L/N`) on the **contour of each part**; **holes** (paths with an odd
nesting depth) get none. `shapes`: SVG shapes marked as tabs (**not milled**), grown by the tool radius
(a marker centred on the edge covers the path, which runs one radius away). The path is split exactly where it enters/leaves a tab
(0.2 mm sampling + bisection), height jumps are **vertical only** (at the plunge feed). Conditions/warnings: height ≥ depth or ≤ 0 → ignored
with a warning; band width > tool → warning and ignored; contour too short (N·W > 0.5·L → N reduced, if < 1 warning);
no marker/no contact → warning. Compatible with the ramp.

### 12.4 `ConverterPage`
Scrolling left column (410–450 px): **SVG file** (Open, name/shapes/size, resolution px/inch default 96, scale %);
**Operations** (list, ＋ Add, Duplicate, Remove, ▲, ▼; **the list starts EMPTY** like jscut and the last one can be removed too;
form: name, type, combine shapes, direction, depth, per pass, step over %, width, offset, ramp, **tabs** [mode,
count, width, height, list of marker shapes visible only in "from shapes" mode], list of the shapes used with check boxes);
**Tool and speeds** (Ø, feed rate, plunge, safe Z, M3/M5 + S, G-code units, zero position; remembered in QSettings);
**G-code** (Generate, Save .gcode…, **Send to CNC ➜**, summary). On the right: 3D preview (with the **original design** under the
path as a guide) and tabs "Generated G-code" (read-only) / "Messages" (SVG, CAM and parser warnings + summary).
Behaviour: every change regenerates after 350 ms in a `QThread` (`GenThread`, old results discarded via a serial number) and
**disables Save/Send until the G-code is up to date** (also when pressing "Generate" by hand); with no operations it shows only the design
with the message "Add an operation…", empty G-code and disabled buttons; the `sendRequested(gcode, name)` signal is handled by the main
window (`load_gcode_text` + switch to Sender mode + a message in the status bar).

### 12.5 `splash.py` and start-up
`SplashScreen(version, duration_ms=3000)`: 780×440 borderless window, `WA_TranslucentBackground`, rounded corners, centred, always
on top; 60 fps animation with `QElapsedTimer`; fade in (350 ms) and fade out (last 300 ms) via the painter's `setOpacity` (not
`windowOpacity`); `finished` after exactly the duration. Drawing: dark gradient background with a cyan glow, **isometric grid**
fading towards the text, **"squircle" spiral toolpath** (7.5 turns) traced progressively, coloured by depth
like the viewer (`depth_color`), orange outline, faint shadow, **cutter** (white shank + cyan tip with halo) following the spiral, XYZ
axes at the bottom, thin cyan→blue **progress bar**. Text on the left: **"CNC"** (white) **"Deck"** (accent) big and black,
gradient rule, below in small **"Stefano Ciurleo & Claude Code - 2026"**, then **"Version {x.y.z}"** in accent, and at the bottom a faint
line "GRBL sender · SVG to G-code converter". `set_time(ms)` freezes the animation (screenshots/tests).
`__main__.main(argv)`: options `file`, `--sim` (auto-connect to the simulator), `--no-splash` (or env `CNCDECK_NO_SPLASH`),
`--screenshot PNG` (development: saves and closes, no splash). With splash: the splash appears **immediately**, the `MainWindow` is prepared behind it and
shown on the `finished` signal. `signal.SIGINT` → default; stylesheet applied to the `QApplication`.

## 13. Tests and acceptance criteria (≈109 tests, all green, ~2 minutes)

Run `python -m pytest -q`. Write tests that verify **behaviours**, not implementation; the main ones:

- **gcode**: line cleaning; bounds and modes (G90/G91); inch units; IJ and R arcs consistent (G3 ⇒ same length), full circle,
  helix; time estimate (100 mm at F600 = 10 s); warnings; command↔line mapping; resume preamble; M6/T → M0; G1 above the work and first
  move as air; extent only from work moves (on the real file `examples/ellipse_cam.gcode`).
- **controller (simulator)**: connection and `$$`; long program (400 lines) **never exceeding 127 bytes**, all `ok`s, final position;
  jog + cancel; WCO zero; pause/resume/stop (position kept, no "sync lost"); error → hold.
- **viewer**: Z up in all views; top view X right/Y up; "grab" orbit (point near the viewer goes right with a right drag, down with a down drag);
  the marker coincides with the path points (identical projection, top and iso views) and Y+ moves it upwards.
- **svg/cam**: import (units, transforms, viewBox, dpi 96/72, curves/arcs, text, broken SVG); exact outside/inside offsets;
  pocket coverage ≤ tool radius; engrave back and forth; holes with evenodd and **no cut crossing the material**;
  combine union/intersection; milling directions (6 cases); origins (3); G-code clean for the sender (no parser warnings, short
  lines, no G2/G3/M6/T, Z within limits); passes; ramp; inches = millimetres; optional spindle; **no path ⇒ empty G-code**;
  tabs (total length, spacing, high passes without tabs, vertical-only jumps, shapes mode, holes excluded, ignored cases,
  ramp); **performance** (SVG parse < 1 s, 150×100 pocket < 20 s).
- **height map**: grid axes and serpentine order; bilinear exact on a plane and clamped outside; shift to work coordinates; JSON round-trip and
  errors; line split at the grid and following the surface; feed/modal independence; relative program emitted absolute and mode restored;
  arcs → segments; inches; refusal of `G92/G10/G55`, unknown position, incomplete map, untouched non-motion lines; speed;
  **probing against the simulator with a tilted surface**: the map equals the surface and the corrected program keeps a constant depth relative to it;
  refusal when `Pn:P` is already closed; clean failure with no contact; parameter validation.
- **UI (offscreen)**: mode switch; step and continuous jog with stop on release; up arrow = Y+; load and run
  to the end; probe Z; writing `$110`; console; macros; pause after M0; error → non-modal bar and exit **without blocking** (also
  when paused for an error); converter with an empty list and disabled buttons; adding/removing an operation; send to the Sender **in memory**
  (no file written, identical program, nothing starts by itself); **generated G-code streamed to the simulator with no errors or overflow**
  and ending at safe Z; 3 s splash with the required texts; `--no-splash`; **height map probing + run on a tilted surface through the UI**.
- In the UI tests: always close the windows you create (otherwise the application event filter errors at the end of the test), use `QApplication`
  (not `QCoreApplication`), stub the `QMessageBox`es, speed up/slow down the simulator depending on what you observe.

**Mandatory visual check**: generate screenshots with `widget.grab().save()` (offscreen) of the running Sender, the converter with
operations and tabs, the splash, the settings dialog, and **look at them**: layout not cut off, buttons with a background, readable text.

## 14. Packaging, documentation, release

- `install.sh` (Ubuntu): `apt` for the system libraries, venv in `~/.local/share/cncdeck/venv`, `pip install .`, link
  `~/.local/bin/cncdeck`, `.desktop` and SVG icon in `~/.local/share`, adds the user to the `dialout` group (needs a logout) and offers to
  remove `brltty` (it steals CH340 boards: the `/dev/ttyUSB0` port disappears).
- `packaging/build-deb.sh`: Debian package using the distribution's Python libraries (only `svgelements` bundled), man page, README.Debian.
- README (English): splash image at the top, "What it is" section with Sender and Converter screenshots, installation, serial
  port on Ubuntu, features table, Converter section (with the optimisations over jscut and what is not implemented), height-map section, development,
  warning to try the real machine with care. Compatibility sentence: **"Tested on Ubuntu 26.04 LTS."**
- `docs/make_screenshots.py` regenerates splash/sender/converter for the README.
- Git: branch `claude/main`; clear commit messages; **one commit per feature**; never leave red tests in commits.

## 15. Binding decisions (do not change without asking)

1. **Axes**: right-handed view, Z up, from above X right and **Y up**; marker = `WPos` with no inversions; **up arrow = Y+**, down = Y−.
   (There is a history of attempts with mirrored views and inverted markers: all wrong. Files with negative Y simply
   appear **below** the origin, which therefore sits at the top left of the part.) No orientation selector, no "invert Y" option,
   no reference to the "bed" in the interface.
2. The converter uses the jscut convention `Y = −Y_svg`, default zero at the top left of the design.
3. "Send to CNC" **does not start** the job. The spindle is **not** switched on by default in the generated G-code.
4. No modal dialog for streaming errors; no "header-only" G-code.
5. The converter's operation list is **empty** at start-up.
6. Streaming is always character-counting with a 127 buffer; stop = hold + reset; pause = `!`; resume = `~`.
7. The height map is stored in machine coordinates and applied only at run time to a copy of the commands; a loaded map starts inactive.

## 16. Known traps (already hit)

- `Signal(dict)` with integer keys → conversion error: use `Signal(object)`.
- A `QMessageBox` for every rejected line, opened inside a slot, stacks up and blocks the window and its closing.
- QSS: an overly generic descendant selector makes `#id` buttons transparent (specificity).
- Sign of `sx` in the projection matrix: wrong ⇒ Z downwards and the scene seen "from below".
- `mergePaths` with empty `bounds`: do not use the interior of the part as a safe area (traversal through the material).
- Clipper raises an exception with an empty subject: guard `difference`/offset.
- `svgelements` `length()` very slow: estimate by sampling.
- Clipper orientation (Y down) ⇒ milling direction inverted: fix and test.
- After a reset, in-flight `ok`s arrive: do not report them as an error for 1.5 s.
- Settings table: a late reload erased the typed values.
- A serial thread that does not end blocked the exit: `terminate()` after the timeout.
- In tests: a second `QApplication` in the same process aborts; `QCoreApplication` is not enough for widgets; unclosed windows leave
  the event filter active on destroyed widgets.
- Commands chained with `&&` and `| tail` do not stop if the tests fail: check the test result before committing.
- Arc with radius == half the chord → `error:34` on the real board (floating-point rounding): avoid it in demo files and warn.
- Height map: a probe that is already closed (`Pn:P`) at the start must be refused; the first descent must start from safe Z; the fast descent
  assumes neighbouring points differ by less than the approach distance (document it, it is a risk of hitting the surface unnoticed).

## 17. Not implemented (state it in the README, do not invent it)

V-pocket; Z origin from the bottom of the material; SVG text conversion (must be converted to paths);
adaptation to macOS/Windows (the project is tested only on Ubuntu 26.04 LTS); manual ordering of paths inside a single
operation (the order is between operations, with ▲/▼; inside an operation it is nearest-neighbour); height-map correction in the
SVG converter (the map applies only to the Sender).
