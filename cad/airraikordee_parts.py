"""
AirRaiKorDee — every 3D-printed part, parametric (CadQuery 2.x).

    python3 airraikordee_parts.py          # writes out/parts/*.stl, out/parts/*.step, out/airraikordee_assembly.step

Coordinates (all mm): Z up, origin = centre of the floor under the unit.
Front = -Y (outlet, camera, sensor grille).  Right = +X (DC jack, stepper).  Rear = +Y (cable channel, limit switch).

Everything that depends on a bought part is in P and marked MEASURE — change it after measuring the real part.
Stack (Z):  base 0-85 | filter section 85-259 | top seat 259-269 | pan ring 269-289 | head 289-381 | head top 381-384
"""
import math, os, importlib.util
import cadquery as cq

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("tc", os.path.join(HERE, "test_coupons.py"))
tc = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(tc)

P = dict(
    # global
    R=100.0, wall=2.5, clr=0.25,
    insert_d=4.0, insert_l=5.7,        # M3 heat-set insert hole  (MEASURE: coupon 04)
    insert_short_l=4.0,                # M3x4 insert where the wall is thin
    m3=3.4, m3_head=6.2, m3_head_h=3.2,
    # base
    base_h=85.0, base_top=3.0, flat_y=88.0, tray_t=3.0,
    jack_d=11.0, jack_z=22.0,          # panel DC jack 5.5x2.5 (MEASURE thread)
    jetson_holes=(86.0, 58.0), jetson_c=(0.0, 42.0), jetson_standoff=16.0,          # Jetson Nano B01 carrier (MEASURE)
    batt=(70.0, 55.0, 40.0), batt_c=(-43.0, -27.5),             # 3S2P pack (MEASURE)
    pms=(50.0, 38.0, 21.0), button_d=16.0,                      # PMS5003, stop button
    # filter section
    filt_r=87.5, filt_h=171.0, filt_hole_d=100.0,               # MEASURE centre hole
    half_h=174.0, band_h=8.0, band_t=4.0, rib_w=7.0, rib_t=3.0, n_ribs=18,
    hinge_r=4.2, hinge_pin=3.2, hinge_axis=(-99.5, 0.0), hinge_gap=0.6,
    magnet_d=6.3, magnet_h=3.2,
    chan_w=22.0, chan_r=(89.0, 94.5),                           # cable channel in rear wall
    seat_t=10.0, foam_groove=(55.0, 65.5, 2.5), seat_hgroove=(16.0, 4.5),
    # pan ring
    ring_h=20.0, module=2.0, ring_teeth=89, pinion_teeth=12, pressure_angle=20.0, backlash=0.25,
    gear_z=(14.0, 20.0), spoke_t=5.0, spoke_w=12.0,
    brg=94.0, brg_hole=54.0, brg_t=10.0, brg_holes=82.0, brg_plate_t=1.5, brg_race_r=37.0, brg_screw_pocket=6.0,         # lazy susan (measured: t 10, hole pitch 82)
    switch_holes=9.5, switch_r=78.0,                            # micro switch (MEASURE)
    # head
    head_h=92.0, top_t=3.0, floor_t=5.0, fan=140.0, fan_h=25.0, fan_holes=124.5,  # fan_h: TL-C14CW measured 25
    stepper_d=28.0, stepper_h=19.0, stepper_off=8.0, stepper_ears=35.0, shaft_d=5.0, shaft_flat=3.0,
    stepper_ear_span=42.0, stepper_ear_w=7.0, stepper_cover=(17.0, 4.0, 16.0),   # MEASURE: ears tip-to-tip, ear width, blue wire box (width, sticks out, height)
    cam_hole=16.0, cam_z=19.0,                                  # MEASURE lens Ø (board camera)
    head_split=33.0,                                            # head = lower (floor, motor, bearing) + upper (outlet, louver)
    cam_win=(64.0, 7.0, 31.0),                                  # camera window in the front wall: width, z0, z1
    cam_board_holes=20.0, cam_board_standoff=5.0,               # MEASURE board camera (square hole pattern, M2); window fits boards <= ~26 tall
    webcam_lens=20.0,                                           # MEASURE webcam lens bezel Ø
    outlet_half_deg=48.6, outlet_z=(38.0, 92.0), shelf_z=(33.0, 37.0),
    # louver cassette
    lv_x=75.0, lv_y=(-62.0, -42.0), lv_z=(37.0, 92.0), vane_chord=13.0, vane_t=2.0, n_vanes=4,
    pivot_d=3.2, crank=11.0, crank_x=52.0, servo_shaft=(-26.0, 64.25),
    servo=(22.8, 12.2, 22.7), servo_ear_pitch=27.6, servo_ear_h=6.8,          # MG90S (MEASURE)
    # charging box
    box=(110.0, 72.0, 36.0), plate_t=3.0, gland_d=12.5, led_d=5.0,
)
Z = dict(base=0.0, half=85.0, seat=85.0 + P["half_h"], ring=85.0 + P["half_h"] + P["seat_t"])
Z["head"] = Z["ring"] + P["ring_h"]
Z["top"] = Z["head"] + P["head_h"]
R, W, C = P["R"], P["wall"], P["clr"]
RI = R - W


# ---------------- helpers ----------------
def pol(r, deg):
    a = math.radians(deg); return (r * math.cos(a), r * math.sin(a))

def cyl(r, h, z0=0.0, x=0.0, y=0.0):
    return cq.Workplane("XY").workplane(offset=z0).center(x, y).circle(r).extrude(h)

def tube(ro, ri, h, z0=0.0):
    return cq.Workplane("XY").workplane(offset=z0).circle(ro).circle(ri).extrude(h)

def box(x0, x1, y0, y1, z0, z1):
    return cq.Workplane("XY").box(x1 - x0, y1 - y0, z1 - z0).translate(((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2))

def xcyl(r, x0, x1, y, z):
    return cq.Workplane("XY").circle(r).extrude(x1 - x0).rotate((0, 0, 0), (0, 1, 0), 90).translate((x0, y, z))

def ycyl(r, y0, y1, x, z):
    return cq.Workplane("XY").circle(r).extrude(y1 - y0).rotate((0, 0, 0), (1, 0, 0), -90).translate((x, y0, z))

def arcseg(ri, ro, a0, a1, z0, z1):
    n = max(6, int(abs(a1 - a0) / 2))
    pts = [pol(ro, a0 + (a1 - a0) * i / n) for i in range(n + 1)] + [pol(ri, a1 - (a1 - a0) * i / n) for i in range(n + 1)]
    return cq.Workplane("XY").workplane(offset=z0).polyline(pts).close().extrude(z1 - z0)

def insert_from_top(x, y, ztop, l=None):
    l = l or P["insert_l"]; return cyl(P["insert_d"] / 2, l + 0.3, ztop - l, x, y)

def insert_from_bottom(x, y, zbot, l=None):
    l = l or P["insert_l"]; return cyl(P["insert_d"] / 2, l + 0.3, zbot - 0.3, x, y)

def screw_down(x, y, ztop, length):
    """M3 clearance hole with a counterbore at the top face."""
    return cyl(P["m3"] / 2, length + 1, ztop - length - 0.5, x, y).union(cyl(P["m3_head"] / 2, P["m3_head_h"] + 0.5, ztop - P["m3_head_h"], x, y))

def screw_up(x, y, zbot, length):
    return cyl(P["m3"] / 2, length + 1, zbot - 0.5, x, y).union(cyl(P["m3_head"] / 2, P["m3_head_h"] + 0.5, zbot - 0.5, x, y))

def clip_posts(x, y, z0, h, along_x=True, gap=None):
    """two small posts with a zip-tie slot: holds a bought part without glue."""
    gap = gap or 24.0
    out = None
    for s in (-1, 1):
        px, py = (x + s * gap / 2, y) if along_x else (x, y + s * gap / 2)
        p = box(px - 2.5, px + 2.5, py - 2.5, py + 2.5, z0, z0 + h).cut(box(px - 3, px + 3, py - 1, py + 1, z0 + h - 6, z0 + h - 3) if along_x else box(px - 1, px + 1, py - 3, py + 3, z0 + h - 6, z0 + h - 3))
        out = p if out is None else out.union(p)
    return out


# =====================================================================
# 1. Charging box (Node 2)
# =====================================================================
BX, BY, BZ = P["box"]
BOX_BOSS = [(sx * (BX / 2 - 5), sy * (BY / 2 - 5)) for sx in (-1, 1) for sy in (-1, 1)]

def charge_box_body():
    h = BZ - P["plate_t"]
    b = box(-BX / 2, BX / 2, -BY / 2, BY / 2, 0, h)
    b = b.cut(box(-BX / 2 + W, BX / 2 - W, -BY / 2 + W, BY / 2 - W, -1, h - W))
    for x, y in BOX_BOSS:
        b = b.union(cyl(3.5, h - W, 0, x, y)).cut(insert_from_bottom(x, y, 0))
    b = b.cut(xcyl(P["jack_d"] / 2, -BX / 2 - 1, -BX / 2 + W + 1, 0, 15))            # adapter jack 5.5x2.1 (left)
    b = b.cut(xcyl(P["gland_d"] / 2, BX / 2 - W - 1, BX / 2 + 1, 0, 15))             # cable gland PG7 (right)
    b = b.cut(cyl(P["led_d"] / 2, W + 2, h - W - 1, -40, 20))                          # status LED window
    for i in range(8):                                                                 # vents over the XL4015
        x = 2 + i * 6
        b = b.cut(box(x, x + 2.5, 8, 30, h - W - 1, h + 1))
    for i in range(5):                                                                 # side vents low on the back
        x = -10 + i * 8
        b = b.cut(box(x, x + 3, BY / 2 - W - 1, BY / 2 + 1, 4, 14))
    return b

def charge_box_plate():
    t = P["plate_t"]
    p = box(-BX / 2, BX / 2, -BY / 2, BY / 2, 0, t)
    for x, y in BOX_BOSS:
        p = p.cut(screw_up(x, y, 0, t))
    # board standoffs (M3 self-tap into Ø2.6). Hole patterns are typical module sizes — MEASURE your boards.
    boards = {"XL4015 54x23": ((18, 17.0), (48.0, 17.0)), "D4184 34x17": ((-28, 16.5), (29.0, 12.0))}
    for _, ((cx, cy), (hx, hy)) in boards.items():
        for sx in (-1, 1):
            for sy in (-1, 1):
                x, y = cx + sx * hx / 2, cy + sy * hy / 2
                p = p.union(cyl(3, 5, t, x, y)).cut(cyl(1.3, 6, t, x, y))
    # cradles (no mounting holes on these boards): XIAO 21x17.5, INA226 ~22x17, mini-360 buck 17x11
    for (cx, cy), (w, d) in {(-32, -18): (21.5, 18.0), (32, -18): (22.5, 17.5), (-6, -22): (17.5, 11.5)}.items():
        p = p.union(box(cx - w / 2 - 1.5, cx + w / 2 + 1.5, cy - d / 2 - 1.5, cy + d / 2 + 1.5, t, t + 3)).cut(box(cx - w / 2, cx + w / 2, cy - d / 2, cy + d / 2, t, t + 4))
    for x, y in [(-50, 0), (-30, 0), (50, 0)]:                                         # rubber-foot recesses... and zip anchors
        pass
    for x, y in [(-BX / 2 + 12, -BY / 2 + 12), (BX / 2 - 12, -BY / 2 + 12), (-BX / 2 + 12, BY / 2 - 12), (BX / 2 - 12, BY / 2 - 12)]:
        p = p.cut(cyl(5.5, 1.0, -0.01, x, y))                                          # Ø11 rubber feet
    return p


# =====================================================================
# 2. Base, electronics tray, front sensor module
# =====================================================================
BH, FY = P["base_h"], P["flat_y"]
BASE_TRAY_BOSS = [pol(88, a) for a in (20, 160, 235, 305)]
HALF_BOSS = [pol(92.5, a) for a in (45, 135)]             # rear filter wall -> base, and top seat -> wall
SENSOR_SCREWS = [(-25.0, 68.0), (25.0, 68.0)]

def base_shell():
    t = P["base_top"]
    b = tube(R, RI, BH).union(cyl(R, t, BH - t))
    b = b.cut(box(-120, 120, -120, -FY, -1, BH + 1))                                  # flat front
    half = math.sqrt(R ** 2 - FY ** 2)
    b = b.union(box(-half, half, -FY, -FY + W, 0, BH).intersect(cyl(R - 0.01, BH)))
    b = b.cut(box(-39.5, 35, -FY - 1, -FY + W + 1, 10, 63))                            # sensor-module window (PMS + button)
    for x, z in SENSOR_SCREWS:
        b = b.union(ycyl(4, -FY + W, -FY + W + 6, x, z)).cut(ycyl(P["insert_d"] / 2, -FY - 0.5, -FY + 6.3, x, z))
    # right: DC jack (with an inside spot-face so the nut sits flat)
    b = b.union(box(RI - 5, RI + 0.5, -9, 9, P["jack_z"] - 9, P["jack_z"] + 9).intersect(cyl(R - 0.01, BH)))
    b = b.cut(xcyl(P["jack_d"] / 2, RI - 6, R + 1, 0, P["jack_z"]))
    # rear: service opening for the rubber flap + 2 inserts above it
    b = b.cut(box(-35, 35, 80, 105, 15, 55))
    for x in (-25, 25):
        y = math.sqrt(RI ** 2 - x ** 2)
        b = b.union(ycyl(4, y - 6, y + 0.5, x, 64)).cut(ycyl(P["insert_d"] / 2, y - 6.3, R + 1, x, 64))
    # side vents beside the Jetson
    for a0 in list(range(22, 62, 6)) + list(range(118, 158, 6)):
        b = b.cut(arcseg(RI - 1, R + 1, a0, a0 + 2.2, 15, 66))
    # tray bosses (inserts from below)
    for x, y in BASE_TRAY_BOSS:
        a = math.degrees(math.atan2(y, x))
        web = box(84, RI + 1.0, -2.5, 2.5, P["tray_t"], P["tray_t"] + 9).rotate((0, 0, 0), (0, 0, 1), a)   # ties the boss to the wall
        b = b.union(cyl(4.5, 9, P["tray_t"], x, y).union(web).intersect(cyl(RI + 1.0, 20))).cut(insert_from_bottom(x, y, P["tray_t"]))
    # top plate: inserts for the rear filter wall, harness slot to the cable channel
    for x, y in HALF_BOSS:
        b = b.union(cyl(4.5, 6, BH - t - 6, x, y).intersect(cyl(RI + 0.01, BH))).cut(insert_from_top(x, y, BH))
    r0, r1 = P["chan_r"]
    b = b.cut(box(-P["chan_w"] / 2, P["chan_w"] / 2, r0, r1, BH - t - 1, BH + 1))
    return b

def base_tray():
    t = P["tray_t"]
    tr = cyl(RI - C, t).cut(box(-120, 120, -120, -FY + W + C, -1, t + 1))
    for x, y in BASE_TRAY_BOSS:
        tr = tr.cut(screw_up(x, y, 0, t))
    for a in (45, 135, 225, 315):                                                      # rubber feet recesses Ø14
        x, y = pol(75, a); tr = tr.cut(cyl(7, 1.0, -0.01, x, y))
    jx, jy = P["jetson_c"]; hx, hy = P["jetson_holes"]                                # Jetson standoffs
    for sx in (-1, 1):
        for sy in (-1, 1):
            x, y = jx + sx * hx / 2, jy + sy * hy / 2
            hs = P["jetson_standoff"]                                                      # Jetson rides above the battery cradle
            tr = tr.union(cyl(3.5, hs, t, x, y)).cut(insert_from_top(x, y, t + hs))
    bx, by = P["batt_c"]; L, Wd, _ = P["batt"]                                        # battery cradle (fire bag inside)
    tr = tr.union(box(bx - L / 2 - 4, bx + L / 2 + 4, by - Wd / 2 - 4, by + Wd / 2 + 4, t, t + 15)
                  .cut(box(bx - L / 2 - 2, bx + L / 2 + 2, by - Wd / 2 - 2, by + Wd / 2 + 2, t, t + 16)).intersect(cyl(RI - C, 40)))
    for x in (bx - 20, bx + 20):                                                       # strap slots for a velcro strap
        tr = tr.cut(box(x - 10, x + 10, by - Wd / 2 - 4.5, by - Wd / 2 - 1.5, -1, t + 1)).cut(box(x - 10, x + 10, by + Wd / 2 + 1.5, by + Wd / 2 + 4.5, -1, t + 1))
    for x in (10, 30, 50, 70):                                                         # mounting grid: ESP32-S3 carrier, ULN2003, bucks, ideal diode
        for y in (-50, -30, -10):
            if math.hypot(x, y) < 82:
                tr = tr.union(cyl(3.5, 5, t, x, y)).cut(insert_from_top(x, y, t + 5, P["insert_short_l"]))
    return tr

def sensor_module():
    """flat plate outside the base flat: grille for the PMS5003, BME280 vents, stop button. PMS cradle on the back."""
    t = 3.0
    pw, pz0, pz1 = 80.0, 5.0, 73.0
    pl = box(-pw / 2, pw / 2, -FY - t, -FY, pz0, pz1)
    L, H, D = P["pms"]
    cx0, cz0 = -38.0 + 1.5, 13.0
    for i in range(7):                                                                  # PMS grille 40 x 20
        x = cx0 + 6 + i * 6
        pl = pl.cut(box(x, x + 3, -FY - t - 1, -FY + 1, cz0 + 10, cz0 + 30))
    for i in range(4):                                                                  # BME280 vents (top-left)
        x = -32 + i * 5
        pl = pl.cut(box(x, x + 2.5, -FY - t - 1, -FY + 1, 55, 61))
    pl = pl.cut(ycyl(P["button_d"] / 2, -FY - t - 1, -FY + 1, 26, 37))
    for x, z in SENSOR_SCREWS:
        pl = pl.cut(ycyl(P["m3"] / 2, -FY - t - 1, -FY + 1, x, z)).cut(ycyl(P["m3_head"] / 2, -FY - t - 1, -FY - t + 1.6, x, z))
    # PMS5003 cradle behind the plate (goes through the window), BME280 shelf
    cr = box(cx0 - 1.5, cx0 + L + 1.5, -FY, -FY + D + 1, cz0 - 1.5, cz0 + H + 1.5).cut(box(cx0, cx0 + L, -FY - 1, -FY + D + 2, cz0, cz0 + H))
    cr = cr.cut(box(cx0 + 10, cx0 + 14, -FY + 8, -FY + 14, cz0 - 2, cz0 + H + 2))       # zip-tie slot
    pl = pl.union(cr).union(box(-33, -12, -FY, -FY + 12, 52, 54))
    pl = pl.union(box(-30, 30, -FY, -FY + W + 2.4, 10.2, 12)).union(box(-30, 30, -FY + W + 0.4, -FY + W + 2.4, 5, 12))  # hook over the window sill
    return pl


# =====================================================================
# 3. Filter section: rear wall, front door, top seat
# =====================================================================
HH = P["half_h"]

def _half(a0, a1, height):
    bt, bh = P["band_t"], P["band_h"]
    s = None
    for z0 in (0.0, height / 2 - bh / 2, height - bh):
        seg = arcseg(R - 2.5 - bt + 2.5, R - 2.5 + 2.5, a0, a1, z0, z0 + bh)               # band r 93.5..97.5 (wall OD 195)
        s = seg if s is None else s.union(seg)
    ang_w = math.degrees(P["rib_w"] / 97.5)
    step = (a1 - a0) / P["n_ribs"]
    for i in range(P["n_ribs"]):
        a = a0 + step * (i + 0.5)
        s = s.union(arcseg(97.5 - P["rib_t"], 97.5, a - ang_w / 2, a + ang_w / 2, 0, height))
    return s

def _hinge_parts(own_knuckles, other_knuckles, height):
    hx, hy = P["hinge_axis"]; r = P["hinge_r"]; g = P["hinge_gap"]
    kh = (height - 3 * g) / 4
    add, cut = None, None
    for k in own_knuckles:
        z0 = k * (kh + g)
        piece = cyl(r, kh, z0, hx, hy)
        add = piece if add is None else add.union(piece)
    for k in other_knuckles:
        z0 = k * (kh + g)
        piece = cyl(r + 0.5, kh + 2 * g, z0 - g, hx, hy)
        cut = piece if cut is None else cut.union(piece)
    pin = cyl(P["hinge_pin"] / 2, height + 2, -1, hx, hy)
    return add, cut, pin

def filter_wall_rear():
    s = _half(0, 180, HH)
    gap = P["hinge_gap"] / 2
    s = s.union(box(90, 97.5, gap, 7.5, 0, HH)).union(box(-97.5, -90, gap, 7.5, 0, HH))         # end posts
    add, cut, pin = _hinge_parts([1, 3], [0, 2], HH)
    s = s.union(add).union(box(-99.5, -96, gap, 4.2, 0, HH).intersect(add.union(box(-97.5, -96, gap, 4.2, 0, HH))))
    s = s.cut(cut).cut(pin)
    for z in (20, HH - 20):                                                                         # magnets
        s = s.cut(ycyl(P["magnet_d"] / 2, gap - 0.01, gap + P["magnet_h"], 93.75, z))
    # cable channel at 90°: thick block with an inward-open U and two retaining lips
    r0, r1 = P["chan_r"]; cw = P["chan_w"]
    blk = box(-cw / 2 - 2.5, cw / 2 + 2.5, r0 - 1, 97.5, 0, HH).intersect(cyl(97.5, HH))
    blk = blk.cut(box(-cw / 2, cw / 2, r0 + 0.5, r1, -1, HH + 1)).cut(box(-cw / 2 + 3, cw / 2 - 3, r0 - 2, r0 + 1, -1, HH + 1))
    s = s.union(blk)
    # bosses: bottom (screw down into base), top (insert for the top seat)
    for x, y in HALF_BOSS:
        b = cyl(4.5, P["band_h"], 0, x, y).union(cyl(4.5, P["band_h"], HH - P["band_h"], x, y)).intersect(cyl(97.5, HH))
        s = s.union(b).cut(screw_down(x, y, P["band_h"], P["band_h"])).cut(insert_from_top(x, y, HH))
    return s

def filter_door_front():
    h = HH - 0.5
    s = _half(180, 360, h)
    gap = P["hinge_gap"] / 2
    s = s.union(box(90, 97.5, -7.5, -gap, 0, h)).union(box(-97.5, -90, -7.5, -gap, 0, h))
    add, cut, pin = _hinge_parts([0, 2], [1, 3], HH)
    s = s.union(add.intersect(box(-110, 0, -10, 10, 0, h))).union(box(-99.5, -96, -4.2, -gap, 0, h).intersect(add.union(box(-97.5, -96, -4.2, -gap, 0, h))))
    s = s.cut(cut).cut(pin)
    for z in (20, HH - 20):
        s = s.cut(ycyl(P["magnet_d"] / 2, -gap - P["magnet_h"], -gap + 0.01, 93.75, z))
    tab = box(96, 104, -12, -3, h / 2 - 12, h / 2 + 12).edges("|Z").fillet(2.5)                      # pull tab
    return s.union(tab)

def seat_top():
    t = P["seat_t"]
    s = cyl(R, t).cut(cyl(P["filt_hole_d"] / 2, t + 2, -1))
    g0, g1, gd = P["foam_groove"]
    s = s.cut(tube(g1, g0, gd))                                                                     # foam groove (underside)
    s = s.cut(cq.Workplane("XY").add(cq.Solid.makeCone(g1, g1 + gd, gd, pnt=cq.Vector(0, 0, 0))).cut(cyl(g1, gd + 1, -0.5)))  # lead-in
    r0, r1 = P["chan_r"]; cw = P["chan_w"]
    s = s.cut(box(-cw / 2, cw / 2, r0, r1, -1, t + 1))                                              # harness slot
    hw, hd = P["seat_hgroove"]
    s = s.cut(box(-hw / 2, hw / 2, 20, r1, t - hd, t + 1))                                           # groove to the centre (top)
    s = s.cut(cyl(hw / 2, hd + 1, t - hd, 0, 20))
    for x, y in HALF_BOSS:
        s = s.cut(screw_down(x, y, t, t))
    for a in RING_SCREW_ANG:
        x, y = pol(95.75, a); s = s.cut(insert_from_top(x, y, t, 6))
    # clearance pockets for the tips of the bearing screws that poke out under the ring spokes (0/180/270°).
    # 6 deep leaves 1.5 mm above the foam groove. Rear (90°) sits in the harness groove: use a short screw there.
    for a in (0, 180, 270):
        x, y = pol(BRG_HOLE_R, a); s = s.cut(cyl(4.0, 7, t - P["brg_screw_pocket"], x, y))
    return s


# =====================================================================
# 4. Pan ring (fixed): internal gear, spoke cross for the bearing, limit switch pad
# =====================================================================
RING_SCREW_ANG = (30, 150, 270)
GP = dict(module=P["module"], pressure_angle=P["pressure_angle"], backlash=P["backlash"])
RP = P["module"] * P["ring_teeth"] / 2
PIN_R = RP - P["module"] * P["pinion_teeth"] / 2                    # = 77 for 89/12
BRG_HOLE_R = P["brg_holes"] / 2 * math.sqrt(2)                      # hole circle radius (57.8)

def pan_ring():
    h = P["ring_h"]; root = RP + 1.25 * P["module"]; tip = RP - P["module"]
    s = tube(R, root, h)
    gz0, gz1 = P["gear_z"]
    g = tube(root + 0.5, tip - 0.5, gz1 - gz0, gz0).cut(tc.ring_gear_cutter(P["ring_teeth"], GP, 40).translate((0, 0, -10)))
    s = s.union(g)
    st, sw = P["spoke_t"], P["spoke_w"]
    for a in (0, 90, 180, 270):                                                                     # bottom bearing plate holes on the axes
        sp = box(BRG_HOLE_R - 6, root + 0.5, -sw / 2, sw / 2, 0, st).union(cyl(6, st, 0, BRG_HOLE_R, 0))
        if a == 90:                                                                                   # rear spoke = harness cover
            sp = sp.union(box(22, root + 0.5, -10, 10, 0, st))
        sp = sp.cut(cyl(P["insert_d"] / 2, st + 1, -0.5, BRG_HOLE_R, 0))
        s = s.union(sp.rotate((0, 0, 0), (0, 0, 1), a))
    # limit switch pad on the rear spoke
    sx, sy = 0, P["switch_r"]
    for d in (-P["switch_holes"] / 2, P["switch_holes"] / 2):
        s = s.cut(cyl(1.1, st + 1, -0.5, d, sy))
    for a in RING_SCREW_ANG:
        x, y = pol(95.75, a); s = s.cut(screw_down(x, y, h, h))
    s = s.cut(box(-P["chan_w"] / 2, P["chan_w"] / 2, P["chan_r"][0], root + 0.6, -1, st - 2))       # harness entry under the rear spoke
    return s

def bearing_sleeve():
    return tube(P["brg_hole"] / 2 - 0.4, 25.0, 10).union(tube(29.0, 25.0, 1.2, 10)).cut(box(-1, 1, 20, 30, -1, 12))


# =====================================================================
# 5. Head body, head top (with deflector), pinion
# =====================================================================
HB = P["head_h"]
FAN_H = P["fan_holes"] / 2
STEP_C = PIN_R + P["stepper_off"]                                    # stepper body centre on +X (85)
TOP_BOSS_ANG = (0, 60, 120, 180)

def stepper_shape(z_face=2.0, cx=None):
    """28BYJ-48 as it really is: can, mounting ears on the SHAFT face, blue wire box on the side AWAY from the shaft,
    boss + shaft 8 mm off-centre toward the head axis (-X). Mounted shaft-down, ears resting on the floor pad."""
    cx = STEP_C if cx is None else cx
    d, h = P["stepper_d"], P["stepper_h"]; span, ew = P["stepper_ear_span"], P["stepper_ear_w"]; cw, cp, ch = P["stepper_cover"]
    sx = cx - P["stepper_off"]
    m = cyl(d / 2, h, z_face, cx, 0).union(box(cx - ew / 2, cx + ew / 2, -span / 2, span / 2, z_face, z_face + 0.8))
    m = m.union(box(cx + d / 2 - 4, cx + d / 2 + cp, -cw / 2, cw / 2, z_face + 1, z_face + 1 + ch))
    shaft = cyl(P["shaft_d"] / 2, 8.5, z_face - 10, sx, 0).intersect(box(sx - P["shaft_flat"] / 2, sx + P["shaft_flat"] / 2, -3, 3, z_face - 10, z_face - 3.5).union(box(sx - 3, sx + 3, -3, 3, z_face - 3.5, z_face)))
    return m.union(cyl(4.5, 1.5, z_face - 1.5, sx, 0)).union(shaft)                                   # D-flats on the last 6 mm


STRAP_POSTS = [(77.0, -27.0), (77.0, 27.0)]

def stepper_strap():
    """clamp plate over the 28BYJ-48 (head coordinates). Holes slotted ±1.5 along X so the motor can slide to set mesh."""
    z0 = 2.0 + P["stepper_h"]
    s = box(72.0, 92.0, -31.5, 31.5, z0, z0 + 3.0).edges("|Z").fillet(3)
    for x, y in STRAP_POSTS:
        s = s.cut(box(x - 1.5, x + 1.5, y - P["m3"] / 2, y + P["m3"] / 2, z0 - 1, z0 + 4)).cut(cyl(P["m3"] / 2, 5, z0 - 1, x - 1.5, y)).cut(cyl(P["m3"] / 2, 5, z0 - 1, x + 1.5, y))
    return s


CAM_SCREW_X = (-38.0, 38.0)
HEAD_JOINT_ANG = (32, 148, 205, 335)          # screw columns lower -> upper head (clear of fan, stepper, camera, loops)


def head_lower():
    """floor, bearing standoffs, stepper, fan, camera window, speaker grille: assemble this first and test the pan."""
    zs = P["head_split"]; ft = P["floor_t"]
    s = head_body().intersect(box(-130, 130, -130, 130, -40, zs))
    for a in HEAD_JOINT_ANG:
        x, y = pol(RI - 5.0, a)
        col = cyl(4.5, zs - ft, ft, x, y).union(box(RI - 6, RI + 0.5, -2.5, 2.5, ft, zs).rotate((0, 0, 0), (0, 0, 1), a))
        s = s.union(col.intersect(cyl(RI + 0.01, 100))).cut(insert_from_top(x, y, zs))
    return s


def head_upper():
    """outlet, louver shelf, top-plate bosses. 4 × M3 down into the lower part."""
    zs = P["head_split"]
    s = head_body().intersect(box(-130, 130, -130, 130, zs, 200))
    for a in HEAD_JOINT_ANG:
        x, y = pol(RI - 5.0, a)
        s = s.union(cyl(4.5, 8, zs, x, y).union(box(RI - 6, RI + 0.5, -2.5, 2.5, zs, zs + 8).rotate((0, 0, 0), (0, 0, 1), a)).intersect(cyl(RI + 0.01, 100)))
        s = s.cut(screw_down(x, y, zs + 8, 8))
    return s


def _cam_plug(lens_d):
    """plug that fills the camera window + outside bezel with 2 screws; slides out to the front."""
    cw, cz0, cz1 = P["cam_win"]; c = C
    plug = box(-cw / 2 + c, cw / 2 - c, -R - 1, -60, cz0 + c, cz1 - c).intersect(cyl(R + 0.2, 100)).cut(cyl(RI - 1.5, 100))
    bezel = box(-cw / 2 - 12, cw / 2 + 12, -R - 3, -60, cz0 - 3, cz1 + 2).intersect(cyl(R + 2.5, 100)).cut(cyl(R + 0.05, 100))
    s = plug.union(bezel).cut(ycyl(lens_d / 2, -R - 5, -60, 0, P["cam_z"]))
    for x in CAM_SCREW_X:
        s = s.cut(ycyl(P["m3"] / 2, -R - 5, -60, x, P["cam_z"]))
    return s


def cam_cassette_board():
    """19a: for the bare USB camera board (HBVCAM): lens hole + 4 M2 standoffs behind it."""
    s = _cam_plug(P["cam_hole"]); h = P["cam_board_holes"] / 2; L = P["cam_board_standoff"]
    for sx in (-1, 1):
        for sz in (-1, 1):
            x, z = sx * h, P["cam_z"] + sz * h
            s = s.union(ycyl(2.5, -RI, -RI + 1.5 + L, x, z).intersect(cyl(RI - 1.4, 100))).cut(ycyl(0.9, -RI + 1, -RI + 2 + L, x, z))
    return s


def cam_cassette_webcam():
    """19b: for a cheap webcam with its shell: bigger lens window + a shelf with 2 zip-tie slots behind."""
    cw, cz0, cz1 = P["cam_win"]
    s = _cam_plug(P["webcam_lens"])
    shelf = box(-cw / 2 + 1, cw / 2 - 1, -RI, -RI + 22, cz0 + C, cz0 + C + 2).intersect(cyl(RI - 1.4, 100))
    for x in (-18, 18):
        shelf = shelf.cut(box(x - 2, x + 2, -RI + 10, -RI + 13, cz0 - 1, cz0 + 4))
    return s.union(shelf)


SWITCH_TAB_Y = (-76.0, -82.0)


def bearing_spacer():
    """20 (x4): spacer between the head floor arms and the lazy-susan TOP plate. M3 from below into the arm insert."""
    sh = P["ring_h"] - P["spoke_t"] - P["brg_t"]
    return cyl(5.0, sh, -sh).cut(cyl(P["m3"] / 2, sh + 2, -sh - 1))


def switch_tab():
    """21: tab under the head front that hits the limit switch at ±180°. 2 × M3 from below into floor inserts."""
    zb = -11.5 - (P["ring_h"] - 20.0)
    s = box(-5, 5, -85, -73, zb, 0)
    for y in SWITCH_TAB_Y:
        s = s.cut(cyl(P["m3"] / 2, -zb + 1, zb - 0.5, 0, y)).cut(cyl(P["m3_head"] / 2, -zb - 3.0, zb - 0.5, 0, y))   # counterbore, 3 mm left
    return s


def head_body():
    ft = P["floor_t"]
    s = tube(R, RI, HB)
    s = s.union(tube(RI, 70, ft))                                                                   # floor ledge (fan corners sit here)
    for a in (45, 135, 225, 315):                                                                   # cross to the bearing top plate
        bar = box(50, 74, -7, 7, 0, ft).rotate((0, 0, 0), (0, 0, 1), a)        # 14 wide arm, flush with the floor (flat underside)
        x, y = pol(BRG_HOLE_R, a)
        s = s.union(bar).union(cyl(6.0, ft, 0, x, y)).cut(insert_from_bottom(x, y, 0, P["insert_short_l"]))   # spacer 20 goes under it
    for sx in (-1, 1):                                                                              # fan screw inserts
        for sy in (-1, 1):
            s = s.cut(insert_from_top(sx * FAN_H, sy * FAN_H, ft, P["insert_short_l"]))
    # stepper pocket (+X): bulge in the wall, radial slot pocket (±1.5) to set gear mesh.
    # NOT held by its ear screws: screw heads under the floor would sit at r 87 = the ring-gear teeth.
    # Held from ABOVE by 18_stepper_strap on two posts (inserts), strap holes slotted radially.
    cw, cp, ch = P["stepper_cover"]
    wire_gap = 3.0                                                                                   # the 5 wires leave the END of the blue box (outward)
    rb = STEP_C + P["stepper_d"] / 2 + cp + 1.5 + C + wire_gap + 2.0                                 # bulge outer radius: box + slot + wire gap + 2 mm wall
    s = s.union(box(RI - 1, rb, -20, 20, 0, P["stepper_h"] + 9).intersect(cyl(rb, 40)).cut(cyl(RI - 1, 50, -1)))
    pocket = None
    for dx in (-0.5, 0.5, 1.5):                                                                      # radial slot -0.5..+1.5 (inward the can would touch the fan)
        m = stepper_shape(2.0, STEP_C + dx)
        m = m.union(cyl(P["stepper_d"] / 2, 40, 2.0, STEP_C + dx, 0))                                  # free to lift out
        pocket = m if pocket is None else pocket.union(m)
    s = s.cut(pocket.intersect(box(0, 200, -50, 50, 2.0 - 0.01, 60)))                                # floor under the motor stays 2 mm
    for sy in (-1, 1):                                                                               # clearance around ears / box
        s = s.cut(box(STEP_C - P["stepper_ear_w"] / 2 - 0.5 - C, STEP_C + P["stepper_ear_w"] / 2 + 1.5 + C, -P["stepper_ear_span"] / 2 - C, P["stepper_ear_span"] / 2 + C, 2.0, 2.0 + 1.5))
    s = s.cut(box(STEP_C + P["stepper_d"] / 2 - 5, STEP_C + P["stepper_d"] / 2 + cp + 1.5 + C + wire_gap, -cw / 2 - C, cw / 2 + C, 2.0, P["stepper_h"] + 10))   # open to the top: wires bend up and back in
    s = s.cut(box(PIN_R - 7, PIN_R + 7, -5, 5, -1, 3)).cut(cyl(5, 4, -1, PIN_R - 2, 0)).cut(cyl(5, 4, -1, PIN_R + 2, 0))  # shaft boss slot ±2
    top = 2.0 + P["stepper_h"] - 0.5                                                                 # post top 0.5 below the motor top -> strap clamps
    for x, y in STRAP_POSTS:
        s = s.union(cyl(4.5, top - ft, ft, x, y)).cut(insert_from_top(x, y, top))
    # limit-switch tab under the front
    for y in SWITCH_TAB_Y:                                                                          # tab 21 screws on from below
        s = s.cut(insert_from_bottom(0, y, 0, P["insert_short_l"]))
    # camera (front), speaker grille (left), rear harness anchors
    cw, cz0, cz1 = P["cam_win"]
    s = s.cut(box(-cw / 2, cw / 2, -R - 1, -60, cz0, cz1).cut(cyl(RI - 2.0, 100)))                 # camera window (cassette 19a/19b)
    for x in CAM_SCREW_X:
        yi = -math.sqrt(RI ** 2 - x ** 2)
        s = s.union(ycyl(4.0, yi - 0.5, yi + 7, x, P["cam_z"]).intersect(cyl(RI + 0.01, 100)))
        s = s.cut(ycyl(P["insert_d"] / 2, -R - 1, yi + 6.5, x, P["cam_z"]))                        # insert pressed in from OUTSIDE
    for y in range(-16, 20, 8):
        for z in range(10, 32, 7):
            s = s.cut(xcyl(1.6, -R - 1, -RI + 1, y, z))
    s = s.union(clip_posts(-82, 0, ft, 22, along_x=False))
    s = s.union(clip_posts(0, 84, ft, 12, gap=18))
    # wiring inside the head: the fan corners leave only ~4 mm to the wall, so wires pass UNDER them in floor
    # grooves, and run along the wall through zip-tie loops (rear pocket -> left: speaker/camera, -> right: stepper/fan/servo)
    for a in (45, 135, 225, 315):
        s = s.cut(box(80, RI - 0.3, -6, 6, ft - 3, ft + 1).rotate((0, 0, 0), (0, 0, 1), a))
    for a in (20, 65, 115, 160, 195, 235, 305, 345):
        z0 = 10.0
        loop = box(RI - 5.5, RI + 0.5, -4, 4, z0, z0 + 8).cut(box(RI - 3.8, RI - 1.2, -2.6, 2.6, z0 - 1, z0 + 9))
        s = s.union(loop.rotate((0, 0, 0), (0, 0, 1), a))
    # outlet + louver shelf
    a0, a1 = 270 - P["outlet_half_deg"], 270 + P["outlet_half_deg"]
    oz0, oz1 = P["outlet_z"]
    s = s.cut(arcseg(RI - 1, R + 2, a0, a1, oz0, oz1 + 1))
    z0, z1 = P["shelf_z"]
    s = s.union(box(-RI, RI, -RI, -58, z0, z1).intersect(cyl(RI + 0.01, 100)))
    for x in (-60, 60):
        s = s.cut(insert_from_top(x, -66, z1, P["insert_short_l"]))
    # bosses for the top plate
    for a in TOP_BOSS_ANG:
        x, y = pol(RI - 2.5, a)
        s = s.union(cyl(4.5, 10, HB - 10, x, y).intersect(cyl(RI + 0.01, 200))).cut(insert_from_top(x, y, HB))
    return s

def head_top():
    t = P["top_t"]
    s = cyl(R, t, HB)
    for a in TOP_BOSS_ANG:
        x, y = pol(RI - 2.5, a); s = s.cut(screw_down(x, y, HB + t, t))
    s = s.cut(cyl(1.5, t + 2, HB - 1, 0, 80))                                                      # INMP441 mic hole (rear)
    for x in (-7, 7):                                                                                # mic board posts (M2)
        s = s.union(cyl(2.2, 4, HB - 4, x, 80)).cut(cyl(0.9, 5, HB - 4.5, x, 80))
    # 45° deflector (printed upside-down with the plate, no supports)
    k = math.sqrt(2) * W
    pl = cq.Workplane("YZ").polyline([(40, HB), (40 + 3.5, HB), (RI, 35 + 3.5), (RI, 35), ]).close().extrude(2 * R).translate((-R, 0, 0))
    defl = pl.intersect(cyl(RI - 0.3, HB - 30, 30))
    defl = defl.cut(box(-12, 12, RI - 10, RI + 1, 34, 42))                                          # cable slot into the dry bay
    for a in TOP_BOSS_ANG:
        x, y = pol(RI - 2.5, a); defl = defl.cut(cyl(5, 20, HB - 12, x, y))
    s = s.union(defl)
    # louver frame sits under the front of the plate: two locating pins
    for x in (-40, 40):
        s = s.union(cyl(1.4, 2.5, HB - 2.5, x, -52))
    return s

def pinion():
    # D-bore grips the shaft flats; the first 2 mm (top face, nearest the motor) is round because the
    # 28BYJ-48 shaft is round for ~2 mm above its 6 mm flats.
    g = tc.external_gear(P["pinion_teeth"], dict(GP, gear_face=P["gear_z"][1] - P["gear_z"][0] - 0.5), bore=P["shaft_d"] + 0.2, d_flat=P["shaft_flat"] + 0.2)
    return g.cut(cyl((P["shaft_d"] + 0.2) / 2, 2.1, -0.1))


# =====================================================================
# 6. Louver cassette: frame + servo bracket, vanes, tie bar, push rod
# =====================================================================
LX = P["lv_x"]; LY0, LY1 = P["lv_y"]; LZ0, LZ1 = P["lv_z"]
PIV_Y = (LY0 + LY1) / 2
VANE_Z = [LZ0 + 7.0 + i * 13.5 for i in range(P["n_vanes"])]

def louver_frame():
    s = box(-LX, LX, LY0, LY1, LZ0, LZ1).cut(box(-LX + 4, LX - 4, LY0 - 1, LY1 + 1, LZ0 + 3, LZ1 - 3))
    s = s.intersect(cyl(RI - 0.3, 200))
    for z in VANE_Z:
        s = s.cut(xcyl(P["pivot_d"] / 2 - 0.15, -LX - 1, LX + 1, PIV_Y, z))                      # press fit for Ø3 pins
    for x in (-60, 60):                                                                             # screw ears onto the shelf
        s = s.union(box(x - 6, x + 6, -72, LY0 + 1, LZ0, LZ0 + 4)).cut(screw_down(x, -66, LZ0 + 4, 4))
    for x in (-40, 40):
        s = s.cut(cyl(1.6, 3, LZ1 - 2.5, x, -52))
    # MG90S bracket behind the right side
    sy, sz = P["servo_shaft"]; L, Wd, H = P["servo"]
    px0 = 64.0
    br = box(px0, px0 + 2.5, -38, -3, sz - 11, sz + 11)
    br = br.union(box(px0, LX, LY1 - 2, -36, sz - 11, sz + 11))
    br = br.cut(box(px0 - 1, px0 + 4, sy - 6.2, sy - 6.2 + L, sz - Wd / 2 - 0.2, sz + Wd / 2 + 0.2))
    ec = sy - 6.2 + L / 2
    for d in (-P["servo_ear_pitch"] / 2, P["servo_ear_pitch"] / 2):
        br = br.cut(xcyl(0.9, px0 - 1, px0 + 4, ec + d, sz))
    return s.union(br)

def vane():
    """printed flat; pivot on the X axis at y=0, crank tab (in-plane) toward +Y at x = crank_x"""
    L = 2 * (LX - 4) - 1.0; ch = P["vane_chord"]; t = P["vane_t"]
    v = box(-L / 2, L / 2, -ch / 2, ch / 2, -t / 2, t / 2).edges("|X").fillet(0.9)
    for x in (-L / 2 + 3, L / 2 - 3):
        v = v.union(xcyl(3.0, x - 3, x + 3, 0, 0))
    v = v.union(box(P["crank_x"], P["crank_x"] + 1.5 + 2, ch / 2 - 1, P["crank"] + 2.5, -t / 2, t / 2)).union(xcyl(2.5, P["crank_x"], P["crank_x"] + 3.5, P["crank"], 0))
    v = v.cut(xcyl(P["pivot_d"] / 2, -L, L, 0, 0)).cut(xcyl(1.1, P["crank_x"] - 1, P["crank_x"] + 5, P["crank"], 0))
    return v

def tie_bar():
    z0, z1 = VANE_Z[0] - 4, VANE_Z[-1] + 4
    s = box(0, 2, -2.5, 2.5, z0, z1)
    for z in VANE_Z + [P["servo_shaft"][1]]:
        s = s.cut(xcyl(1.1, -1, 3, 0, z))
    return s

def push_rod():
    L = P["servo_shaft"][0] - PIV_Y
    s = box(0, L, -2.0, 2.0, 0, 2).union(cyl(2.0, 2, 0, 0, 0)).union(cyl(2.0, 2, 0, L, 0))
    return s.cut(cyl(1.1, 4, -1, 0, 0)).cut(cyl(1.1, 4, -1, L, 0))


# =====================================================================
# bought-part placeholders (for fit checks only)
# =====================================================================
def placeholders():
    ph = {}
    ph["filter"] = tube(P["filt_r"], P["filt_hole_d"] / 2, P["filt_h"], Z["half"])
    ph["fan"] = box(-70, 70, -70, 70, Z["head"] + P["floor_t"], Z["head"] + P["floor_t"] + P["fan_h"]).edges("|Z").fillet(14).cut(cyl(68, 40, Z["head"]))
    b = P["brg"] / 2
    # lazy susan = two thin square plates + a round ball race. Bottom plate (corners on the axes) is FIXED to the ring;
    # top plate (corners on the diagonals) turns WITH the head, so it never moves relative to the pinion.
    z0 = Z["ring"] + P["spoke_t"]; z1 = z0 + P["brg_t"]; pt = P["brg_plate_t"]; rr = P["brg_race_r"]
    hole = cyl(P["brg_hole"] / 2, 300)
    ph["bearing"] = box(-b, b, -b, b, z0, z0 + pt).rotate((0, 0, 0), (0, 0, 1), 45).union(tube(rr, P["brg_hole"] / 2, P["brg_t"] - pt, z0 + pt)).cut(hole)
    ph["bearing_top"] = box(-b, b, -b, b, z1 - pt, z1).cut(cyl(rr + 0.3, 300))
    ph["stepper"] = stepper_shape(2.0).translate((0, 0, Z["head"]))
    sy, sz = P["servo_shaft"]; L, Wd, H = P["servo"]
    ph["servo"] = box(64.0 + 2.5 - P["servo_ear_h"], 64.0 + 2.5 - P["servo_ear_h"] + H, sy - 6.2, sy - 6.2 + L, Z["head"] + sz - Wd / 2, Z["head"] + sz + Wd / 2)
    jx, jy = P["jetson_c"]
    ph["jetson"] = box(jx - 50, jx + 50, jy - 40, jy + 40, P["tray_t"] + P["jetson_standoff"], P["tray_t"] + P["jetson_standoff"] + 45)
    bx, by = P["batt_c"]; L, Wd, H = P["batt"]
    ph["battery"] = box(bx - L / 2, bx + L / 2, by - Wd / 2, by + Wd / 2, P["tray_t"], P["tray_t"] + H)
    L, H, D = P["pms"]
    ph["pms5003"] = box(-36.5, -36.5 + L, -FY + 0.3, -FY + D + 0.3, 13, 13 + H)
    return ph


# =====================================================================
PARTS = {   # name: (builder, placement in the assembly (dx,dy,dz, rot_x_deg), qty, print note)
    "01_charge_box_body":   (charge_box_body,   (260, 70, P["plate_t"], 0), 1, "upside-down (top on the bed)"),
    "02_charge_box_plate":  (charge_box_plate,  (260, 70, 0, 0),            1, "flat"),
    "03_base_shell":        (base_shell,        (0, 0, 0, 0),              1, "upside-down (top plate on the bed); support only under the side vents' tops is not needed"),
    "04_base_tray":         (base_tray,         (0, 0, 0, 0),              1, "flat"),
    "05_sensor_module":     (sensor_module,     (0, 0, 0, 0),              1, "front face on the bed"),
    "06_filter_wall_rear":  (filter_wall_rear,  (0, 0, Z["half"], 0),      1, "upright, bottom band on the bed, no supports"),
    "07_filter_door_front": (filter_door_front, (0, 0, Z["half"], 0),      1, "upright, bottom band on the bed, no supports"),
    "08_seat_top":          (seat_top,          (0, 0, Z["seat"], 0),      1, "top face on the bed (groove for the harness up)"),
    "09_pan_ring":          (pan_ring,          (0, 0, Z["ring"], 0),      1, "spokes on the bed"),
    "10_bearing_sleeve":    (bearing_sleeve,    (0, 0, Z["ring"] + P["spoke_t"] + 0.5, 0), 1, "flange up"),
    "11a_head_lower":       (head_lower,        (0, 0, Z["head"], 0),      1, "floor down; tree supports under the floor cross + switch tab"),
    "11b_head_upper":       (head_upper,        (0, 0, Z["head"], 0),      1, "joint face down (z 33), outlet up, no supports"),
    "12_head_top":          (head_top,          (0, 0, Z["head"], 0),      1, "upside-down (plate on the bed), deflector is 45°"),
    "13_pinion_12T":        (pinion,            (PIN_R, 0, Z["head"] - 0.5, 180), 1, "flat, hub down"),
    "14_louver_frame":      (louver_frame,      (0, 0, Z["head"], 0),      1, "front face on the bed"),
    "15_vane":              (vane,              (0, PIV_Y, Z["head"], 0),  4, "flat"),
    "16_tie_bar":           (tie_bar,           (P["crank_x"] - 5.0, PIV_Y + P["crank"], Z["head"], 0), 1, "flat"),
    "17_push_rod":          (push_rod,          (0, 0, 0, 0),              1, "flat"),
    "18_stepper_strap":     (stepper_strap,     (0, 0, Z["head"], 0),      1, "flat"),
    "19a_cam_board":        (cam_cassette_board,  (0, 0, Z["head"], 0),    1, "bezel face on the bed"),
    "19b_cam_webcam":       (cam_cassette_webcam, (0, 0, Z["head"], 0),    0, "bezel face on the bed (alternative to 19a, not in the assembly)"),
    "20_bearing_spacer":    (bearing_spacer,    (0, 0, Z["head"], 0),      4, "flat"),
    "21_switch_tab":        (switch_tab,        (0, 0, Z["head"], 0),      1, "on its side"),
}


def build_all():
    return {n: spec[0]() for n, spec in PARTS.items()}


def place(name, shape, vane_i=0, head_rot=0.0):
    dx, dy, dz, _ = PARTS[name][1]
    s = shape
    if name == "15_vane":
        s = s.translate((0, 0, VANE_Z[vane_i]))
    if name == "20_bearing_spacer":
        x, y = pol(BRG_HOLE_R, 45 + 90 * vane_i); s = s.translate((x, y, 0))
    if name == "17_push_rod":
        s = s.rotate((0, 0, 0), (0, 0, 1), 90).translate((P["crank_x"] + 0.5, PIV_Y + P["crank"], Z["head"] + P["servo_shaft"][1] - 1))
    if PARTS[name][1][3]:
        s = s.rotate((0, 0, 0), (1, 0, 0), PARTS[name][1][3])   # flipped: top face ends at z = 0
    s = s.translate((dx, dy, dz))
    if name[:2] in ("11", "12", "13", "14", "15", "16", "17", "18", "19", "20", "21") and head_rot:
        s = s.rotate((0, 0, 0), (0, 0, 1), head_rot)
    return s


if __name__ == "__main__":
    out = os.path.join(HERE, "out", "parts"); os.makedirs(out, exist_ok=True)
    shapes = build_all()
    asm = cq.Assembly(name="AirRaiKorDee")
    # sub-assemblies: open in Fusion 360 / FreeCAD and each group is one clickable component
    GROUPS = {"1_charging_box": ("01", "02"), "2_base": ("03", "04", "05", "jetson", "battery", "pms5003"),
              "3_filter_section": ("06", "07", "08", "filter"), "4_pan_ring_fixed": ("09", "10", "bearing"),
              "5_head_rotating": ("11", "12", "13", "14", "15", "16", "17", "18", "19", "20", "21", "fan", "stepper", "servo", "bearing_top")}
    sub = {g: cq.Assembly(name=g) for g in GROUPS}
    def grp(key):
        for g, keys in GROUPS.items():
            if key in keys:
                return sub[g]
        return asm
    for n, s in shapes.items():
        bb = s.val().BoundingBox()
        dims = sorted([bb.xlen, bb.ylen, bb.zlen])
        ok = dims[2] <= 220 and dims[1] <= 220
        print(f"{n:22s} {bb.xlen:6.1f} x {bb.ylen:6.1f} x {bb.zlen:6.1f}  {s.val().Volume()/1000:7.1f} cm3  x{PARTS[n][2]}  {'ok' if ok else 'TOO BIG'}")
        cq.exporters.export(s, os.path.join(out, n + ".stl"), tolerance=0.03, angularTolerance=0.15)
        cq.exporters.export(s, os.path.join(out, n + ".step"))
        for i in range(PARTS[n][2]):
            grp(n[:2]).add(place(n, s, i), name=f"{n}_{i+1}" if PARTS[n][2] > 1 else n,
                    color=cq.Color(0.85, 0.55, 0.15) if n[:2] in ("11", "12", "13", "14", "15", "16", "17", "18", "19", "20", "21") else cq.Color(0.55, 0.62, 0.62))
    for n, s in placeholders().items():
        grp(n).add(s, name="bought_" + n, color=cq.Color(0.2, 0.45, 0.8, 0.6))
    for g in GROUPS:
        asm.add(sub[g], name=g)
    asm.save(os.path.join(HERE, "out", "airraikordee_assembly.step"))
    print("done ->", out)
