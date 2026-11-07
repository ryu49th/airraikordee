import math, cadquery as cq, importlib.util, sys
spec=importlib.util.spec_from_file_location("tc","test_coupons.py"); tc=importlib.util.module_from_spec(spec); spec.loader.exec_module(tc)
P=tc.PARAMS
jig=tc.gear_jig_plate(P)
ring_only=jig.intersect(tc.sector(85-0.6, 100, 46, 6).translate((0,0,3)))
pin=tc.external_gear(12,P,bore=5.2)
for th in [0,7.5,15,22.5]:
    a_ring=th*12/89   # rolling relation for internal mesh (same direction)
    p=pin.rotate((0,0,0),(0,0,1),th).translate((77,0,3))
    r=ring_only.rotate((0,0,0),(0,0,1),a_ring)
    v=r.intersect(p).val().Volume() if True else 0
    print(f"pinion {th:5.1f}deg  overlap {v:7.3f} mm3")
