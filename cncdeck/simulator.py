"""A tiny GRBL 1.1 emulator exposing a pyserial-like API, for demos and tests.

Models what matters for a sender: the 127-byte RX buffer, a 15-block planner that gates `ok`,
status reports, hold/resume, jog + jog-cancel, work coordinate offsets, probing and overrides.
Motion is simulated at `speed` times real time.
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import deque

WORD = re.compile(r"([A-Z])([-+]?(?:\d+\.?\d*|\.\d+))")

DEFAULT_SETTINGS = {
    0: 10, 1: 25, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 10: 1, 11: 0.010, 12: 0.002, 13: 0,
    20: 0, 21: 0, 22: 0, 23: 0, 24: 25.0, 25: 500.0, 26: 250, 27: 1.0, 30: 1000, 31: 0, 32: 0,
    100: 250.0, 101: 250.0, 102: 250.0, 110: 2000.0, 111: 2000.0, 112: 800.0,
    120: 200.0, 121: 200.0, 122: 100.0, 130: 160.0, 131: 100.0, 132: 45.0,
}


class _Block:
    __slots__ = ("target", "feed", "rapid", "jog", "probe", "dwell", "pause")

    def __init__(self, target, feed, rapid=False, jog=False, probe=False, dwell=0.0, pause=False):
        self.pause = pause
        self.target = target
        self.feed = feed
        self.rapid = rapid
        self.jog = jog
        self.probe = probe
        self.dwell = dwell


class SimulatedSerial:
    def __init__(self, speed: float = 6.0, boot_delay: float = 0.2):
        self.speed = speed
        self.timeout = 0.005
        self.is_open = True
        self.overflows = 0          # bytes dropped because the host overran the RX buffer
        self.max_rx_used = 0
        self.lines_executed = 0
        self.log: list[str] = []
        self.settings = dict(DEFAULT_SETTINGS)
        self._out = bytearray()
        self._cv = threading.Condition()
        self._lock = threading.Lock()
        self._rx_lines: deque[str] = deque()
        self._line = bytearray()
        self._rx_used = 0
        self._planner: deque[_Block] = deque()
        self._pos = [0.0, 0.0, 0.0]
        self._coords = {54: [0.0] * 3, 55: [0.0] * 3, 56: [0.0] * 3, 57: [0.0] * 3, 58: [0.0] * 3, 59: [0.0] * 3}
        self._g92 = [0.0] * 3
        self._cs = 54
        self._abs = True
        self._inch = False
        self._feed = 0.0
        self._modal_motion = 0
        self._spindle = "M5"
        self._s = 0.0
        self._hold = False
        self._alarm = False
        self._check = False
        self._homing = False
        self._ov_feed = 100
        self._ov_rapid = 100
        self._ov_spindle = 100
        self._reports = 0
        self._probe_z = -18.0       # virtual probe plate contact (machine Z)
        self._busy_until = 0.0
        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        self._emit_later("\r\nGrbl 1.1h ['$' for help]\r\n", boot_delay)

    # ---------- pyserial-like API ----------
    @property
    def in_waiting(self) -> int:
        with self._cv:
            return len(self._out)

    def read(self, size: int = 1) -> bytes:
        with self._cv:
            if not self._out and self.timeout:
                self._cv.wait(self.timeout)
            data = bytes(self._out[:size])
            del self._out[:size]
            return data

    def write(self, data: bytes) -> int:
        with self._lock:
            for b in data:
                self._rx_byte(b)
        return len(data)

    def close(self) -> None:
        self.is_open = False
        self._stop = True

    # ---------- helpers ----------
    def _emit(self, text: str) -> None:
        with self._cv:
            self._out += text.encode()
            self._cv.notify_all()

    def _emit_later(self, text: str, delay: float) -> None:
        threading.Timer(delay, self._emit, args=(text,)).start()

    def _wco(self):
        return [self._coords[self._cs][i] + self._g92[i] for i in range(3)]

    def _state(self) -> str:
        if self._alarm:
            return "Alarm"
        if self._hold:
            return "Hold:0"
        if self._homing:
            return "Home"
        if self._check:
            return "Check"
        if self._planner:
            return "Jog" if self._planner[0].jog else "Run"
        return "Idle"

    # ---------- serial input ----------
    def _rx_byte(self, b: int) -> None:
        c = chr(b)
        if c == "?":
            self._status_report()
        elif c == "!":
            if not self._alarm and self._planner:
                self._hold = True
        elif c == "~":
            self._hold = False
        elif c == "\x18":
            self._soft_reset()
        elif b == 0x85:
            if self._planner and self._planner[0].jog:
                self._planner.clear()
                self._hold = False
        elif 0x90 <= b <= 0x9E or b in (0xA0, 0xA1):
            self._override(b)
        else:
            if self._rx_used >= 127:
                self.overflows += 1
                return
            self._rx_used += 1
            self.max_rx_used = max(self.max_rx_used, self._rx_used)
            if c == "\n":
                self._rx_lines.append(self._line.decode("ascii", "replace"))
                self._line = bytearray()
            elif c != "\r":
                self._line.append(b)

    def _override(self, b: int) -> None:
        clamp = lambda v, lo, hi: max(lo, min(hi, v))  # noqa: E731
        if b == 0x90: self._ov_feed = 100
        elif b == 0x91: self._ov_feed = clamp(self._ov_feed + 10, 10, 200)
        elif b == 0x92: self._ov_feed = clamp(self._ov_feed - 10, 10, 200)
        elif b == 0x93: self._ov_feed = clamp(self._ov_feed + 1, 10, 200)
        elif b == 0x94: self._ov_feed = clamp(self._ov_feed - 1, 10, 200)
        elif b == 0x95: self._ov_rapid = 100
        elif b == 0x96: self._ov_rapid = 50
        elif b == 0x97: self._ov_rapid = 25
        elif b == 0x99: self._ov_spindle = 100
        elif b == 0x9A: self._ov_spindle = clamp(self._ov_spindle + 10, 10, 200)
        elif b == 0x9B: self._ov_spindle = clamp(self._ov_spindle - 10, 10, 200)
        elif b == 0x9C: self._ov_spindle = clamp(self._ov_spindle + 1, 10, 200)
        elif b == 0x9D: self._ov_spindle = clamp(self._ov_spindle - 1, 10, 200)

    def _soft_reset(self) -> None:
        moving = bool(self._planner) and not self._hold
        self._planner.clear()
        self._rx_lines.clear()
        self._line = bytearray()
        self._rx_used = 0
        self._hold = False
        self._homing = False
        self._spindle, self._s = "M5", 0.0
        self._alarm = moving
        self._emit("\r\nGrbl 1.1h ['$' for help]\r\n")
        if moving:
            self._emit("ALARM:3\r\n")

    def _status_report(self) -> None:
        self._reports += 1
        wco = self._wco()
        mpos = self._pos
        s = f"<{self._state()}|MPos:{mpos[0]:.3f},{mpos[1]:.3f},{mpos[2]:.3f}"
        free = 15 - len(self._planner)
        s += f"|Bf:{free},{127 - self._rx_used}"
        cur = self._planner[0] if self._planner else None
        f = (cur.feed * self._ov_feed / 100 if cur and not cur.rapid else 0)
        s += f"|FS:{f:.0f},{self._s * self._ov_spindle / 100 if self._spindle != 'M5' else 0:.0f}"
        if self._reports % 10 == 1:
            s += f"|WCO:{wco[0]:.3f},{wco[1]:.3f},{wco[2]:.3f}"
        if self._reports % 5 == 0:
            s += f"|Ov:{self._ov_feed},{self._ov_rapid},{self._ov_spindle}"
        s += ">\r\n"
        self._emit(s)

    # ---------- worker ----------
    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop:
            now = time.monotonic()
            dt = now - last
            last = now
            with self._lock:
                self._parse_pending()
                self._advance(dt)
            time.sleep(0.002)

    def _parse_pending(self) -> None:
        while self._rx_lines and len(self._planner) < 15:
            line = self._rx_lines.popleft()
            self._rx_used = max(0, self._rx_used - (len(line) + 1))
            self.log.append(line)
            self._exec_line(line.strip())

    def _exec_line(self, line: str) -> None:
        line = re.sub(r"\([^)]*\)", "", line).split(";")[0].strip().upper().replace(" ", "")
        if not line:
            self._emit("ok\r\n")
            return
        if line.startswith("$"):
            return self._exec_system(line)
        if self._alarm:
            self._emit("error:9\r\n")
            return
        self.lines_executed += 1
        words = [(l, float(n)) for l, n in WORD.findall(line)]
        vals: dict[str, float] = {}
        gs: list[float] = []
        ms: list[float] = []
        for l, v in words:
            if l == "G": gs.append(v)
            elif l == "M": ms.append(v)
            else: vals[l] = v
        err = self._apply_modal(gs, ms, vals)
        if err:
            self._emit(f"error:{err}\r\n")
            return
        self._emit("ok\r\n")

    def _apply_modal(self, gs, ms, vals, jog=False):
        k = 25.4 if self._inch else 1.0
        probe = False
        non_move = False
        dwell = 0.0
        absolute = self._abs
        for g in gs:
            if g in (0, 1, 2, 3): self._modal_motion = int(g)
            elif g in (38.2, 38.3, 38.4, 38.5): probe = True
            elif g == 20: self._inch = True; k = 25.4
            elif g == 21: self._inch = False; k = 1.0
            elif g == 90: self._abs = absolute = True
            elif g == 91: self._abs = absolute = False
            elif g in (54, 55, 56, 57, 58, 59): self._cs = int(g)
            elif g == 4: dwell = vals.get("P", 0.0)
            elif g == 10 or g == 92:
                non_move = True
                self._apply_offset(g, vals, k)
            elif g == 53: pass
            elif g in (17, 18, 19, 93, 94, 40, 49, 80, 28, 30, 92.1): pass
            else: return 20
        for m in ms:
            if m in (3, 4): self._spindle = f"M{int(m)}"
            elif m == 5: self._spindle = "M5"
            elif m == 0:
                tail = self._planner[-1].target if self._planner else self._pos
                self._planner.append(_Block(list(tail), 0.0, pause=True))
        if "S" in vals: self._s = vals["S"]
        if "F" in vals: self._feed = vals["F"] * k
        if non_move:
            return 0
        if any(a in vals for a in "XYZ") or dwell:
            if self._modal_motion in (1, 2, 3) and self._feed <= 0 and not jog and not probe:
                return 22
            wco = self._wco()
            if 53.0 in gs:
                base = [0.0] * 3
            else:
                base = wco
            cur_w = [self._pos[i] - base[i] for i in range(3)]
            if self._planner:
                last = self._planner[-1].target
                cur_w = [last[i] - base[i] for i in range(3)]
            tgt = list(cur_w)
            for i, a in enumerate("XYZ"):
                if a in vals:
                    tgt[i] = vals[a] * k if (absolute or 53.0 in gs) else cur_w[i] + vals[a] * k
            mtgt = [tgt[i] + base[i] for i in range(3)]
            rapid = self._modal_motion == 0 and not jog
            self._planner.append(_Block(mtgt, self._feed if not rapid else 0.0, rapid=rapid,
                                        jog=jog, probe=probe, dwell=dwell))
        return 0

    def _apply_offset(self, g, vals, k):
        if g == 92:
            for i, a in enumerate("XYZ"):
                if a in vals:
                    self._g92[i] = self._pos[i] - self._coords[self._cs][i] - vals[a] * k
        elif g == 10:
            p = int(vals.get("P", 0)) or (self._cs - 53)
            cs = 53 + p
            if vals.get("L") == 20 and cs in self._coords:
                for i, a in enumerate("XYZ"):
                    if a in vals:
                        self._coords[cs][i] = self._pos[i] - self._g92[i] - vals[a] * k

    def _exec_system(self, line: str) -> None:
        if line == "$$":
            for n in sorted(self.settings):
                v = self.settings[n]
                self._emit(f"${n}={v:g}\r\n" if v == int(v) else f"${n}={v:.3f}\r\n")
            self._emit("ok\r\n")
        elif line == "$G":
            self._emit(f"[GC:G0 G{self._cs} G17 G{'20' if self._inch else '21'} G{'90' if self._abs else '91'} "
                       f"G94 M0 {self._spindle} M9 T0 F{self._feed:g} S{self._s:g}]\r\nok\r\n")
        elif line == "$I":
            self._emit("[VER:1.1h.20190825:Simulatore]\r\n[OPT:V,15,128]\r\nok\r\n")
        elif line == "$#":
            self._emit("ok\r\n")
        elif line == "$X":
            self._alarm = False
            self._emit("[MSG:Caution: Unlocked]\r\nok\r\n")
        elif line == "$H":
            self._homing = True
            self._planner.clear()
            self._planner.append(_Block([0.0, 0.0, 0.0], 800.0))
            self._planner[0].jog = False
            self._emit("ok\r\n")
        elif line == "$C":
            self._check = not self._check
            self._emit("[MSG:Enabled]\r\nok\r\n" if self._check else "[MSG:Disabled]\r\nok\r\n")
        elif line.startswith("$J="):
            if self._alarm or self._state() not in ("Idle", "Jog"):
                self._emit("error:8\r\n")
                return
            words = [(l, float(n)) for l, n in WORD.findall(line[3:])]
            gs = [v for l, v in words if l == "G"]
            vals = {l: v for l, v in words if l not in "GM"}
            if "F" not in vals:
                self._emit("error:22\r\n")
                return
            self._apply_modal(gs, [], vals, jog=True)
            self._emit("ok\r\n")
        elif re.fullmatch(r"\$\d+=[-+0-9.]+", line):
            n, v = line[1:].split("=")
            self.settings[int(n)] = float(v)
            self._emit("ok\r\n")
        else:
            self._emit("error:3\r\n")

    def _advance(self, dt: float) -> None:
        if not self._planner or self._hold or self._alarm:
            return
        if self._check:
            self._planner.clear()
            return
        blk = self._planner[0]
        if blk.pause:
            self._planner.popleft()
            self._hold = True
            return
        if blk.dwell > 0:
            blk.dwell -= dt * self.speed
            if blk.dwell <= 0:
                self._planner.popleft()
            return
        d = [blk.target[i] - self._pos[i] for i in range(3)]
        dist = math.sqrt(sum(x * x for x in d))
        if blk.rapid or blk.feed <= 0:
            rate = min(self.settings[110], self.settings[111]) * self._ov_rapid / 100
        else:
            rate = blk.feed * self._ov_feed / 100
        if self._homing:
            rate = 3000
        step = rate / 60.0 * dt * self.speed
        if blk.probe:
            nxt_z = self._pos[2] + (d[2] / dist * step if dist else 0)
            if nxt_z <= self._probe_z < self._pos[2] + 1e-9:
                self._pos[2] = self._probe_z
                self._planner.clear()
                self._emit(f"[PRB:{self._pos[0]:.3f},{self._pos[1]:.3f},{self._pos[2]:.3f}:1]\r\n")
                return
        if dist <= step:
            self._pos = list(blk.target)
            self._planner.popleft()
            if self._homing and not self._planner:
                self._homing = False
            if blk.probe:
                self._planner.clear()
                self._alarm = True
                self._emit("ALARM:5\r\n")
        else:
            for i in range(3):
                self._pos[i] += d[i] / dist * step
