import numpy as np, matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt, importlib.util, cadquery as cq
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
spec=importlib.util.spec_from_file_location("ap","airraikordee_parts.py"); ap=importlib.util.module_from_spec(spec); spec.loader.exec_module(ap)
S=ap.build_all(); PH=ap.placeholders()
EXP={"03":0,"04":-40,"05":0,"06":60,"07":60,"08":120,"09":160,"10":160,"11":220,"13":220,"14":220,"15":220,"16":220,"17":220,"12":300}
def tri(s,tol=0.6):
    v,t=s.val().tessellate(tol,0.6); V=np.array([[p.x,p.y,p.z] for p in v]); return V[np.array(t)]
def add(ax,T,col):
    n=np.cross(T[:,1]-T[:,0],T[:,2]-T[:,0]); n/=np.linalg.norm(n,axis=1,keepdims=True)+1e-9
    sh=np.clip(n@np.array([.35,-.6,.7]),0,1)*.55+.4
    pc=Poly3DCollection(T,edgecolor="none"); pc.set_facecolor(np.c_[np.outer(sh,col).clip(0,1),np.ones(len(sh))]); ax.add_collection3d(pc)
fig=plt.figure(figsize=(16,9),facecolor="white")
for k,(title,cut,explode) in enumerate([("exploded",False,True),("section (front-right quarter removed)",True,False)]):
    ax=fig.add_subplot(1,2,k+1,projection="3d")
    cutter=ap.box(0,150,-150,0,-10,420)
    items=[]
    for n,s in S.items():
        if n[:2] in ("01","02"): continue
        for i in range(ap.PARTS[n][2]):
            items.append((n,ap.place(n,s,i)))
    for n,s in PH.items(): items.append(("ph_"+n,s))
    for n,s in items:
        if cut: 
            s=s.cut(cutter)
            if not s.vals(): continue
        if explode: s=s.translate((0,0,EXP.get(n[:2],0) if not n.startswith("ph_") else {"filter":60,"fan":220,"bearing":160,"stepper":220,"servo":220}.get(n[3:],0)))
        col=np.array([.35,.55,.95]) if n.startswith("ph_") else (np.array([.95,.62,.25]) if n[:2]>="11" else np.array([.45,.65,.68]))
        try: add(ax,tri(s),col)
        except Exception as e: pass
    zmax=720 if explode else 400
    ax.set_xlim(-200,200); ax.set_ylim(-200,200); ax.set_zlim(0 if not explode else -50,zmax if not explode else 650)
    ax.set_box_aspect((1,1,(zmax+50)/400 if explode else 1)); ax.view_init(18,-55); ax.set_axis_off(); ax.set_title(title)
plt.tight_layout(); plt.savefig("out/assembly_preview.png",dpi=85)
