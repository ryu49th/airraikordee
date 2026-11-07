"""Interference check of the assembled AirRaiKorDee (printed parts + bought-part placeholders)."""
import itertools, importlib.util, cadquery as cq
spec=importlib.util.spec_from_file_location("ap","airraikordee_parts.py"); ap=importlib.util.module_from_spec(spec); spec.loader.exec_module(ap)
S=ap.build_all()
ROT=("11a_head_lower","11b_head_upper","12_head_top","13_pinion_12T","14_louver_frame","15_vane","16_tie_bar","17_push_rod","18_stepper_strap","19a_cam_board","20_bearing_spacer","21_switch_tab")
def placed(rot=0.0):
    out={}
    for n,s in S.items():
        for i in range(ap.PARTS[n][2]):
            sh=s
            if n=="13_pinion_12T" and rot: sh=s.rotate((0,0,0),(0,0,1),+rot*ap.P["ring_teeth"]/ap.P["pinion_teeth"])  # + because the pinion is flipped about X when placed
            out[f"{n}#{i}" if ap.PARTS[n][2]>1 else n]=ap.place(n,sh,i,rot)
    for n,s in ap.placeholders().items():
        if n in ("stepper","servo","fan","bearing_top") and rot: s=s.rotate((0,0,0),(0,0,1),rot)
        out["bought_"+n]=s
    return out
def bb_hit(a,b):
    A=a.val().BoundingBox(); B=b.val().BoundingBox()
    return not (A.xmax<B.xmin or B.xmax<A.xmin or A.ymax<B.ymin or B.ymax<A.ymin or A.zmax<B.zmin or B.zmax<A.zmin)
def vol(a,b):
    try: return a.intersect(b).val().Volume()
    except Exception: return -1
pl=placed(0)
print("== static assembly, overlaps > 0.5 mm3 ==")
bad=0
for (na,a),(nb,b) in itertools.combinations(pl.items(),2):
    if na.startswith("bought") and nb.startswith("bought"): continue
    if not bb_hit(a,b): continue
    v=vol(a,b)
    if v>0.5 or v<0: print(f"{na:24s} x {nb:24s} {v:9.1f}"); bad+=1
print("pairs flagged:",bad)
print("== head rotation vs fixed parts ==")
FIX=["09_pan_ring","08_seat_top","06_filter_wall_rear","07_filter_door_front","bought_bearing"]
for rot in (0,30,90,135,179):
    p=placed(rot)
    for r in ROT+("bought_stepper","bought_servo","bought_fan","bought_bearing_top"):
        for k in [k for k in p if k.split("#")[0]==r]:
            for f in FIX:
                if r in ("11a_head_lower","bought_bearing_top","20_bearing_spacer") and f=="bought_bearing": continue
                if bb_hit(p[k],p[f]):
                    v=vol(p[k],p[f])
                    if v>0.5 or v<0: print(f"rot {rot:4d}  {k:22s} x {f:20s} {v:8.2f}")
print("done")
