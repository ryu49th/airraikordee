"""
AirRaiKorDee — print test coupons (CadQuery).

Print these small parts FIRST to tune clearances for the team's printer before printing the big shells.
All sizes in mm. Change PARAMS, run `python3 test_coupons.py`, STL/STEP files appear in ./out/.

Coupons
  1 gear_jig_plate     internal ring-gear segment (module 2, 89T, as on the pan ring) + an arc slot at r = 77
  2 pinion_12T_jig     12-tooth pinion with a Ø4.6 peg -> peg into the slot, roll the pinion along the ring by hand
  3 pinion_12T_28byj   same pinion with the 28BYJ-48 D-bore (Ø5, flats 3) -> final part if 1+2 mesh well
  4 insert_test        4 holes for M3 heat-set inserts, Ø3.8 / 4.0 / 4.2 / 4.4 (left -> right, 1-4 notches)
  5 fit_test           holes for a Ø6 peg with +0.15 / 0.25 / 0.35 / 0.45 clearance + magnet pockets 6x3
  6 peg_6mm            the Ø6 peg for coupon 5
  7 hinge_leaf_door    2 knuckles, Ø3.2 pin hole (Ø3 steel rod)
  8 hinge_leaf_wall    1 knuckle that fits between them
  9 seat_segment       40° piece of the filter seat: foam groove + chamfered lead-in
"""
import math, os
import cadquery as cq

PARAMS = dict(
    module=2.0, ring_teeth=89, pinion_teeth=12, pressure_angle=20.0,
    gear_face=6.0,          # gear tooth height (Z)
    backlash=0.25,          # total backlash added at the pitch circle (printed gears need some)
    clearance=0.25,         # default assembly clearance
    shaft_d=5.0, shaft_flat=3.0,   # 28BYJ-48 output shaft
)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT, exist_ok=True)


# ---------------- involute gear outline ----------------
def inv(a):
    return math.tan(a) - a


def gear_outline(z, m, pa_deg, r_tip, r_root, extra_half_angle=0.0, pts=12):
    """Closed CCW polygon of an external-tooth gear. extra_half_angle widens (+) or thins (-) each tooth."""
    pa = math.radians(pa_deg)
    rp = m * z / 2
    rb = rp * math.cos(pa)
    half_p = math.pi / (2 * z) + inv(pa) + extra_half_angle

    def psi(r):
        r = max(r, rb)
        return half_p - inv(math.acos(rb / r))

    r0 = max(r_root, rb)
    radii = [r0 + (r_tip - r0) * i / pts for i in range(pts + 1)]
    poly = []
    for k in range(z):
        c = 2 * math.pi * k / z
        if r_root < rb:
            poly.append((r_root, c - psi(rb)))
        for r in radii:                      # flank 1, root -> tip
            poly.append((r, c - psi(r)))
        for r in reversed(radii):            # flank 2, tip -> root
            poly.append((r, c + psi(r)))
        if r_root < rb:
            poly.append((r_root, c + psi(rb)))
    return [(r * math.cos(a), r * math.sin(a)) for r, a in poly]


def external_gear(z, p, bore=None, d_flat=None):
    m = p["module"]; rp = m * z / 2
    half_bl = (p["backlash"] / 2) / rp / 2        # thin each tooth by half the backlash
    outline = gear_outline(z, m, p["pressure_angle"], rp + m, rp - 1.25 * m, -half_bl)
    g = cq.Workplane("XY").polyline(outline).close().extrude(p["gear_face"])
    hub = cq.Workplane("XY").circle(rp - 1.25 * m - 0.01).extrude(p["gear_face"] + 4)   # taller hub for grip
    g = g.union(hub)
    if bore:
        cut = cq.Workplane("XY").circle(bore / 2).extrude(50)
        if d_flat:
            cut = cut.intersect(cq.Workplane("XY").rect(d_flat, bore + 2).extrude(50))
        g = g.cut(cut)
    return g


def ring_gear_cutter(z, p, height):
    """Solid whose outline is the *space* of an internal gear (teeth of a z-tooth external gear, inverted depths)."""
    m = p["module"]; rp = m * z / 2
    half_bl = (p["backlash"] / 2) / rp / 2        # widen spaces by half the backlash
    outline = gear_outline(z, m, p["pressure_angle"], rp + 1.25 * m, rp - m, +half_bl, pts=8)
    return cq.Workplane("XY").polyline(outline).close().extrude(height)


def sector(r_in, r_out, deg, h):
    a = math.radians(deg / 2)
    pts = [(r_in * math.cos(-a), r_in * math.sin(-a))]
    n = 24
    for i in range(n + 1):
        t = -a + 2 * a * i / n
        pts.append((r_out * math.cos(t), r_out * math.sin(t)))
    for i in range(n + 1):
        t = a - 2 * a * i / n
        pts.append((r_in * math.cos(t), r_in * math.sin(t)))
    return cq.Workplane("XY").polyline(pts[:-1]).close().extrude(h)


# ---------------- coupons ----------------
def gear_jig_plate(p):
    """Ring-gear segment + an ARC SLOT at r = 77. The pinion's peg rides in the slot, so the pinion can roll along
    the fixed ring exactly like in the real head (its centre moves on the r = 77 circle). A fixed post would jam it."""
    z = p["ring_teeth"]; m = p["module"]; rp = m * z / 2
    a_center = rp - m * p["pinion_teeth"] / 2                    # centre distance = 77 for 89/12, m2
    base = sector(a_center - 14, rp + 1.25 * m + 4, 46, 3)
    ring = sector(rp - m - 0.5, rp + 1.25 * m + 4, 46, p["gear_face"]).translate((0, 0, 3))
    ring = ring.cut(ring_gear_cutter(z, p, 40).translate((0, 0, -5)))
    slot_w = 5.2                                                   # peg 4.6 -> 0.3 clearance each side
    slot = sector(a_center - slot_w / 2, a_center + slot_w / 2, 24, 10).translate((0, 0, -2))   # ±12°: pinion stays fully meshed
    for sgn in (-1, 1):                                            # round slot ends
        a = math.radians(12 * sgn)
        slot = slot.union(cq.Workplane("XY").center(a_center * math.cos(a), a_center * math.sin(a)).circle(slot_w / 2).extrude(10).translate((0, 0, -2)))
    return base.union(ring).cut(slot)


def pinion_jig(p):
    """12T pinion without hub + a Ø4.6 peg on one face. Print peg UP; flip it so the peg drops into the jig slot."""
    g = external_gear(p["pinion_teeth"], p)
    g = g.cut(cq.Workplane("XY").circle(30).extrude(20).translate((0, 0, p["gear_face"])))   # drop the hub
    peg = cq.Workplane("XY").circle(2.3).extrude(3.0).translate((0, 0, p["gear_face"]))
    return g.union(peg)


def insert_test(p):
    blk = cq.Workplane("XY").box(64, 14, 8).translate((0, 0, 4))
    for i, d in enumerate([3.8, 4.0, 4.2, 4.4]):
        x = -24 + i * 16
        blk = blk.cut(cq.Workplane("XY").center(x, 0).circle(d / 2).extrude(6).translate((0, 0, 2.01)))
        for n in range(i + 1):                       # 1..4 notches on the front edge
            blk = blk.cut(cq.Workplane("XY").box(1, 1.2, 8).translate((x - 3 + n * 2, -7, 4)))
    return blk


def fit_test(p):
    pl = cq.Workplane("XY").box(64, 30, 6).translate((0, 0, 3))
    for i, c in enumerate([0.15, 0.25, 0.35, 0.45]):
        x = -24 + i * 16
        pl = pl.cut(cq.Workplane("XY").center(x, 6).circle((6 + c) / 2).extrude(10).translate((0, 0, -2)))
        md = [6.1, 6.2, 6.3, 6.4][i]
        pl = pl.cut(cq.Workplane("XY").center(x, -7).circle(md / 2).extrude(3.2).translate((0, 0, 2.81)))
        for n in range(i + 1):
            pl = pl.cut(cq.Workplane("XY").box(1, 1.2, 6).translate((x - 3 + n * 2, -15, 3)))
    return pl


def peg_6mm(p):
    return cq.Workplane("XY").circle(3).extrude(14).faces(">Z").chamfer(0.6)


def hinge_leaf(p, door=True):
    od, pin = 8.4, 3.2
    L, gap = 18.0, 0.4
    leaf = cq.Workplane("XY").box(40, 3, 3 * L + 2 * gap).translate((-20 - od / 2 + 1, 0, (3 * L + 2 * gap) / 2))
    zs = [0, 2 * (L + gap)] if door else [L + gap]
    for z0 in zs:
        k = cq.Workplane("XY").circle(od / 2).extrude(L).translate((0, 0, z0))
        leaf = leaf.union(k)
    # remove the leaf where the other part's knuckles sit
    other = [L + gap] if door else [0, 2 * (L + gap)]
    for z0 in other:
        leaf = leaf.cut(cq.Workplane("XY").circle(od / 2 + gap).extrude(L + 2 * gap).translate((0, 0, z0 - gap)))
    leaf = leaf.cut(cq.Workplane("XY").circle(pin / 2).extrude(200).translate((0, 0, -50)))
    return leaf


def seat_segment(p):
    seg = sector(58, 98, 40, 8)
    groove = sector(67, 77, 44, 3).translate((0, 0, 5.01))        # foam groove 10 wide x 3 deep
    seg = seg.cut(groove)
    # chamfered lead-in on the front (+X) top edge so the filter rides onto the foam
    # = a 45° ramp on the outer wall of the groove (r 77 -> 80) the filter cap slides down onto the foam
    ramp = cq.Workplane("XY").add(cq.Solid.makeCone(77, 80, 3, pnt=cq.Vector(0, 0, 5.01)))
    ramp = ramp.cut(cq.Workplane("XY").circle(67).extrude(20))
    seg = seg.cut(ramp)
    return seg


def pogo_test(p):
    blk = cq.Workplane("XY").box(40, 12, 10).translate((0, 0, 5))
    for i, d in enumerate([1.2, 1.3, 1.4, 1.5]):
        x = -15 + i * 10
        blk = blk.cut(cq.Workplane("XY").center(x, 0).circle(d / 2).extrude(12).translate((0, 0, -1)))
        for n in range(i + 1):
            blk = blk.cut(cq.Workplane("XY").box(0.8, 1, 10).translate((x - 2 + n * 1.5, -6, 5)))
    return blk


PARTS = {
    "01_gear_jig_plate": lambda: gear_jig_plate(PARAMS),
    "02_pinion_12T_jig": lambda: pinion_jig(PARAMS),
    "03_pinion_12T_28byj": lambda: external_gear(PARAMS["pinion_teeth"], PARAMS, bore=PARAMS["shaft_d"] + 0.2,
                                                 d_flat=PARAMS["shaft_flat"] + 0.2).cut(
        cq.Workplane("XY").circle((PARAMS["shaft_d"] + 0.2) / 2).extrude(2.1).translate((0, 0, -0.1))),   # round first 2 mm, like the real part
    "04_insert_test": lambda: insert_test(PARAMS),
    "05_fit_test": lambda: fit_test(PARAMS),
    "06_peg_6mm": lambda: peg_6mm(PARAMS),
    "07_hinge_leaf_door": lambda: hinge_leaf(PARAMS, True),
    "08_hinge_leaf_wall": lambda: hinge_leaf(PARAMS, False),
    "09_seat_segment": lambda: seat_segment(PARAMS),
}

if __name__ == "__main__":
    BED = 220
    asm = cq.Assembly()
    for name, fn in PARTS.items():
        shape = fn()
        bb = shape.val().BoundingBox()
        ok = bb.xlen <= BED and bb.ylen <= BED and bb.zlen <= BED
        print(f"{name:24s} {bb.xlen:6.1f} x {bb.ylen:6.1f} x {bb.zlen:5.1f} mm  volume {shape.val().Volume()/1000:6.1f} cm3  {'fits bed' if ok else 'TOO BIG'}")
        cq.exporters.export(shape, os.path.join(OUT, name + ".stl"), tolerance=0.02, angularTolerance=0.1)
        asm.add(shape, name=name)
    asm.save(os.path.join(OUT, "test_coupons.step"))
    print("written to", OUT)
