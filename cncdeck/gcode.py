"""G-code parsing: sanitising for GRBL, toolpath generation, bounds and time estimate."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np

WORD_RE = re.compile(r"([A-Z])([-+]?(?:\d+\.?\d*|\.\d+))")
PAREN_RE = re.compile(r"\([^)]*\)")
LINENO_RE = re.compile(r"^N\d+")
TOOL_RE = re.compile(r"T\d+")
M6_RE = re.compile(r"M0*6(?!\d)")

RAPID, FEED = 0, 1
MAX_LINE_LEN = 80  # GRBL line buffer (incl. terminator)


def clean_line(raw: str) -> str:
    """Strip comments/whitespace/line numbers so the line is as short as possible for GRBL."""
    s = PAREN_RE.sub("", raw).split(";", 1)[0]
    s = "".join(s.split()).upper().lstrip("/")
    if s.startswith("%"):
        return ""
    s = LINENO_RE.sub("", s)
    if not s.startswith("$"):
        # GRBL 1.1 rejects T/M6 (error:20): turn a tool change into a program pause for a manual swap
        s = M6_RE.sub("M0", TOOL_RE.sub("", s))
    return s


@dataclass
class Toolpath:
    start: np.ndarray  # (M,3) float32, mm
    end: np.ndarray    # (M,3)
    kind: np.ndarray   # (M,) uint8 RAPID/FEED
    line: np.ndarray   # (M,) int32 index of the source line

    @classmethod
    def empty(cls) -> "Toolpath":
        return cls(np.zeros((0, 3), np.float32), np.zeros((0, 3), np.float32),
                   np.zeros(0, np.uint8), np.zeros(0, np.int32))

    def __len__(self) -> int:
        return len(self.kind)


@dataclass
class Program:
    source: list[str] = field(default_factory=list)
    commands: list[tuple[int, str]] = field(default_factory=list)  # (source line, cleaned command)
    path: Toolpath = field(default_factory=Toolpath.empty)
    bounds_min: tuple[float, float, float] = (0.0, 0.0, 0.0)
    bounds_max: tuple[float, float, float] = (0.0, 0.0, 0.0)
    feed_length: float = 0.0
    rapid_length: float = 0.0
    line_time: np.ndarray = field(default_factory=lambda: np.zeros(0))
    cum_time: np.ndarray = field(default_factory=lambda: np.zeros(1))
    max_spindle: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def total_time(self) -> float:
        return float(self.cum_time[-1]) if len(self.cum_time) else 0.0

    @property
    def size(self) -> tuple[float, float, float]:
        return tuple(b - a for a, b in zip(self.bounds_min, self.bounds_max))  # type: ignore

    def time_remaining(self, source_line: int) -> float:
        """Estimated seconds left after `source_line` (index) has been executed."""
        if not len(self.cum_time):
            return 0.0
        i = max(0, min(source_line + 1, len(self.cum_time) - 1))
        return float(self.cum_time[-1] - self.cum_time[i])


def _split_words(cmd: str):
    gs: list[float] = []
    ms: list[float] = []
    vals: dict[str, float] = {}
    for letter, num in WORD_RE.findall(cmd):
        v = float(num)
        if letter == "G":
            gs.append(v)
        elif letter == "M":
            ms.append(v)
        else:
            vals[letter] = v
    return gs, ms, vals


def _arc_points(p0, p1, center_a, center_b, a, b, lin, cw, tol=0.02):
    """Points (excluding start) of an arc in plane axes (a,b) with linear axis `lin`."""
    ca, cb = center_a, center_b
    ra = p0[a] - ca
    rb = p0[b] - cb
    r = math.hypot(ra, rb)
    a0 = math.atan2(rb, ra)
    a1 = math.atan2(p1[b] - cb, p1[a] - ca)
    travel = a1 - a0
    eps = 1e-9
    if cw:
        if travel >= -eps:
            travel -= 2 * math.pi
    else:
        if travel <= eps:
            travel += 2 * math.pi
    if r < 1e-9:
        return np.array([p1], dtype=np.float64)
    step = 2 * math.acos(max(0.0, 1 - min(tol, r) / r)) if r > tol else math.pi / 4
    n = int(min(2000, max(2, math.ceil(abs(travel) / max(step, 1e-3)))))
    t = np.linspace(0, 1, n + 1)[1:]
    ang = a0 + travel * t
    pts = np.empty((n, 3))
    pts[:, a] = ca + r * np.cos(ang)
    pts[:, b] = cb + r * np.sin(ang)
    pts[:, lin] = p0[lin] + (p1[lin] - p0[lin]) * t
    pts[-1] = p1  # land exactly on the target
    return pts


def arc_center_from_radius(pa, pb, ta, tb, r, cw):
    """Centre of an R-format arc from (pa,pb) to (ta,tb); caller guarantees a valid radius."""
    x = ta - pa
    y = tb - pb
    d2 = x * x + y * y
    hh = -math.sqrt(max(4 * r * r - d2, 0.0)) / math.sqrt(d2)
    if not cw:
        hh = -hh
    if r < 0:
        hh = -hh
    return pa + 0.5 * (x - y * hh), pb + 0.5 * (y + x * hh)


def parse_program(text: str, rapid_rates=(500.0, 500.0, 500.0), default_feed=0.0) -> Program:
    lines = text.splitlines()
    prog = Program(source=lines)
    pos = np.zeros(3)
    absolute = True
    inch = False
    plane = 17
    motion = None
    feed = default_feed

    starts: list[np.ndarray] = []
    ends: list[np.ndarray] = []
    kinds: list[int] = []
    src: list[int] = []
    line_time = np.zeros(len(lines))
    warn = prog.warnings
    unsupported_seen = set()
    moved = False   # becomes True after the first real motion (the start position is unknown before)

    def add_seg(p0, p1, kind, i):
        starts.append(np.array(p0, dtype=np.float32))
        ends.append(np.array(p1, dtype=np.float32))
        kinds.append(kind)
        src.append(i)

    for i, raw in enumerate(lines):
        s = clean_line(raw)
        if not s:
            continue
        prog.commands.append((i, s))
        if M6_RE.search(PAREN_RE.sub("", raw).upper()):
            warn.append(f"Line {i + 1}: tool change (M6) converted to an M0 pause: change the tool and press Resume")
        if len(s) + 1 > MAX_LINE_LEN - 1:
            warn.append(f"Line {i + 1}: {len(s)} characters, too long for GRBL (max {MAX_LINE_LEN - 2})")
        if s.startswith("$"):
            continue
        gs, ms, vals = _split_words(s)
        k = 25.4 if inch else 1.0
        non_modal_move = False
        dwell = 0.0
        for g in gs:
            if g in (0, 1, 2, 3):
                motion = int(g)
            elif g in (38.2, 38.3, 38.4, 38.5):
                motion = 1
            elif g == 4:
                dwell = vals.get("P", 0.0)
            elif g in (17, 18, 19):
                plane = int(g)
            elif g == 20:
                inch = True
                k = 25.4
            elif g == 21:
                inch = False
                k = 1.0
            elif g == 90:
                absolute = True
            elif g == 91:
                absolute = False
            elif g == 53:
                non_modal_move = True
            elif g in (28, 30):
                non_modal_move = True
            elif g in (10, 92, 92.1, 92.2, 92.3, 54, 55, 56, 57, 58, 59, 93, 94, 90.1, 91.1,
                       40, 49, 43.1, 80, 61):
                pass
            elif g not in unsupported_seen:
                unsupported_seen.add(g)
                warn.append(f"Line {i + 1}: G{g:g} not supported by GRBL 1.1")
        if "F" in vals:
            feed = vals["F"] * k
        if "S" in vals:
            prog.max_spindle = max(prog.max_spindle, vals["S"])
        line_time[i] += dwell

        has_axis = any(c in vals for c in "XYZ")
        if not has_axis or motion is None or non_modal_move:
            continue
        # note: G92/G10 with axis words are not motions
        if any(g in (10, 92) for g in gs):
            continue
        tgt = pos.copy()
        for idx, c in enumerate("XYZ"):
            if c in vals:
                tgt[idx] = vals[c] * k if absolute else pos[idx] + vals[c] * k

        if motion in (0, 1):
            kind = RAPID if motion == 0 else FEED
            if kind == FEED and ((tgt[2] > 0 and pos[2] > 0) or not moved):
                kind = RAPID    # CAM often positions with G1 above the work; the very first move starts from
                                # an unknown position: draw both as air moves
            dist = float(np.linalg.norm(tgt - pos))
            if dist > 0:
                moved = True
                add_seg(pos, tgt, kind, i)
                if motion == 0:
                    prog.rapid_length += dist
                    d = np.abs(tgt - pos)
                    line_time[i] += float(max(d[j] / max(rapid_rates[j], 1.0) for j in range(3))) * 60.0
                else:
                    prog.feed_length += dist
                    if feed > 0:
                        line_time[i] += dist / feed * 60.0
                    else:
                        warn.append(f"Line {i + 1}: G1 move with no feed rate (F)")
            pos = tgt
        else:
            a, b, lin, oa, ob = {17: (0, 1, 2, "I", "J"), 18: (2, 0, 1, "K", "I"), 19: (1, 2, 0, "J", "K")}[plane]
            cw = motion == 2
            if "R" in vals:
                r = vals["R"] * k
                x = tgt[a] - pos[a]
                y = tgt[b] - pos[b]
                d2 = x * x + y * y
                h = 4 * r * r - d2
                if d2 == 0 or h < 0:
                    warn.append(f"Line {i + 1}: arc with invalid radius")
                    pos = tgt
                    continue
                if h < 0.01:
                    warn.append(f"Line {i + 1}: radius almost equal to half the chord, GRBL may reject the arc (error:34)")
                ca, cb = arc_center_from_radius(pos[a], pos[b], tgt[a], tgt[b], r, cw)
            else:
                ca = pos[a] + vals.get(oa, 0.0) * k
                cb = pos[b] + vals.get(ob, 0.0) * k
            pts = _arc_points(pos, tgt, ca, cb, a, b, lin, cw)
            prev = pos
            length = 0.0
            for p in pts:
                add_seg(prev, p, FEED, i)
                length += float(np.linalg.norm(p - prev))
                prev = p
            prog.feed_length += length
            if feed > 0:
                line_time[i] += length / feed * 60.0
            else:
                warn.append(f"Line {i + 1}: arc with no feed rate (F)")
            pos = tgt

    if starts:
        prog.path = Toolpath(np.array(starts, np.float32), np.array(ends, np.float32),
                             np.array(kinds, np.uint8), np.array(src, np.int32))
        work = prog.path.kind == FEED          # size of the part = cutting moves only (not air moves)
        sel = work if work.any() else np.ones(len(prog.path), bool)
        allpts = np.concatenate([prog.path.start[sel], prog.path.end[sel]])
        prog.bounds_min = tuple(float(v) for v in allpts.min(axis=0))
        prog.bounds_max = tuple(float(v) for v in allpts.max(axis=0))
    prog.line_time = line_time
    prog.cum_time = np.concatenate([[0.0], np.cumsum(line_time)])
    return prog


def preamble_for(program: Program, source_line: int) -> list[str]:
    """Commands that restore modal state so the job can resume from `source_line` safely.

    Replays everything before the line, tracking modal state, then returns: units, distance mode,
    plane, feed, spindle, and a move to the XY of the resume point (after raising Z to the highest
    Z seen so the tool clears the work) followed by the plunge to the target Z.
    """
    inch = False
    absolute = True
    plane = 17
    feed = None
    spindle = None  # ("M3", S)
    s_val = None
    pos = np.zeros(3)
    max_z = 0.0
    motion = None
    for i, cmd in program.commands:
        if i >= source_line:
            break
        gs, ms, vals = _split_words(cmd)
        k = 25.4 if inch else 1.0
        for g in gs:
            if g in (0, 1, 2, 3):
                motion = g
            elif g == 20:
                inch, k = True, 25.4
            elif g == 21:
                inch, k = False, 1.0
            elif g == 90:
                absolute = True
            elif g == 91:
                absolute = False
            elif g in (17, 18, 19):
                plane = int(g)
        for m in ms:
            if m in (3, 4):
                spindle = f"M{int(m)}"
            elif m == 5:
                spindle = None
        if "F" in vals:
            feed = vals["F"]
        if "S" in vals:
            s_val = vals["S"]
        if motion is not None:
            for idx, c in enumerate("XYZ"):
                if c in vals:
                    pos[idx] = vals[c] * k if absolute else pos[idx] + vals[c] * k
            max_z = max(max_z, pos[2])
    out = [("G20" if inch else "G21"), f"G{plane}", "G90"]
    if spindle:
        out.append(f"{spindle}S{s_val:g}" if s_val is not None else spindle)
    if feed:
        out.append(f"G1F{feed:g}")
    safe = max_z + 5.0 if max_z > 0 else 5.0
    unit = 1 / 25.4 if inch else 1.0
    out.append(f"G0Z{safe * unit:.4f}")
    out.append(f"G0X{pos[0] * unit:.4f}Y{pos[1] * unit:.4f}")
    out.append(f"G1Z{pos[2] * unit:.4f}")
    if not absolute:
        out.append("G91")
    return out


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
