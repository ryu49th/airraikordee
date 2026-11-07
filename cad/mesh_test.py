import importlib.util
spec=importlib.util.spec_from_file_location("ap","airraikordee_parts.py"); ap=importlib.util.module_from_spec(spec); spec.loader.exec_module(ap)
ring=ap.place("09_pan_ring",ap.pan_ring()); pin0=ap.pinion(); k=ap.P["ring_teeth"]/ap.P["pinion_teeth"]
for rot in (0,7,15,40):
  for sgn in (1,-1):
    pp=ap.place("13_pinion_12T",pin0.rotate((0,0,0),(0,0,1),sgn*rot*k),0,rot)
    print(rot,sgn,round(ring.intersect(pp).val().Volume(),2))
