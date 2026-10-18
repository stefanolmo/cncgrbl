"""Generates examples/demo.nc — a small test job: pocketed plate, holes, engraving, arcs."""
import math

out = ["(CNC Deck demo - fresa 3 mm, materiale: MDF/legno tenero)", "G21 G90 G17", "G0 Z5", "M3 S800"]
w, h = 90.0, 60.0


def rect(x0, y0, x1, y1, z, f=300):
    out.append(f"G0 X{x0:.3f} Y{y0:.3f}")
    out.append(f"G1 Z{z:.3f} F120")
    for x, y in ((x1, y0), (x1, y1), (x0, y1), (x0, y0)):
        out.append(f"G1 X{x:.3f} Y{y:.3f} F{f}")
    out.append("G0 Z5")


# engraving frame, two passes
for z in (-0.5, -1.0):
    rect(5, 5, w - 5, h - 5, z)

# spiral pocket (circle, centre 30,30)
cx, cy = 30.0, 30.0
out.append(f"G0 X{cx} Y{cy}")
out.append("G1 Z-1.5 F100")
for i in range(1, 81):
    a = i * math.pi / 10
    r = (i / 80) * 14
    out.append(f"G1 X{cx + r * math.cos(a):.3f} Y{cy + r * math.sin(a):.3f} F350")
out.append("G3 X44 Y30 I-14 J0 F300")
out.append("G0 Z5")

# helical holes
for hx, hy in ((65, 15), (75, 15), (65, 45), (75, 45)):
    out.append(f"G0 X{hx + 3} Y{hy}")
    out.append("G1 Z0 F100")
    for k in range(1, 4):
        out.append(f"G3 X{hx + 3} Y{hy} Z{-1.0 * k:.1f} I-3 J0 F200")
    out.append(f"G3 X{hx + 3} Y{hy} I-3 J0 F200")
    out.append("G0 Z5")

# S-curve with arcs, R-format
out.append("G0 X52 Y10")
out.append("G1 Z-0.8 F100")
out.append("G2 X52 Y30 R12 F250")
out.append("G3 X52 Y50 R12 F250")
out.append("G0 Z5")

# zig-zag hatching
out.append("G0 X10 Y50")
for i in range(8):
    out.append("G1 Z-0.3 F100" if i == 0 else "")
    out.append(f"G1 X{10 + (i + 1) * 3} Y{50 if i % 2 else 55} F300")
out.append("G0 Z5")
out += ["M5", "G0 X0 Y0", "M2"]
open("examples/demo.nc", "w").write("\n".join(l for l in out if l) + "\n")
