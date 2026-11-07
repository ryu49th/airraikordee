import numpy as np, matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt, importlib.util
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
spec=importlib.util.spec_from_file_location("ap","airraikordee_parts.py"); ap=importlib.util.module_from_spec(spec); spec.loader.exec_module(ap)
S=ap.build_all(); PH=ap.placeholders()
def tri(s,tol=0.4):
    v,t=s.val().tessellate(tol,0.4); V=np.array([[p.x,p.y,p.z] for p in v]); return V[np.array(t,dtype=int)] if len(t) else np.zeros((0,3,3))
fig=plt.figure(figsize=(16,8),facecolor="white")
for k,(cutbox,view,title) in enumerate([(ap.box(-150,150,-150,-30,200,420),(8,-90),"head + ring, front removed (y < -30)"),
                                         (ap.box(-30,150,-150,150,200,420),(8,180),"head + ring, right side removed (x > -30)")]):
    ax=fig.add_subplot(1,2,k+1,projection="3d")
    for n,s in S.items():
        if n[:2] not in ("08","09","10","11","12","13","14","15","16","17"): continue
        for i in range(ap.PARTS[n][2]):
            p=ap.place(n,s,i).cut(cutbox)
            if not p.vals(): continue
            col=np.array([.95,.62,.25]) if n[:2]>="11" else np.array([.5,.68,.7])
            T=tri(p); nn=np.cross(T[:,1]-T[:,0],T[:,2]-T[:,0]); nn/=np.linalg.norm(nn,axis=1,keepdims=True)+1e-9
            sh=np.clip(np.abs(nn@np.array([.3,-.7,.6])),0,1)*.6+.35
            pc=Poly3DCollection(T,edgecolor="none"); pc.set_facecolor(np.c_[np.outer(sh,col).clip(0,1),np.ones(len(sh))]); ax.add_collection3d(pc)
    for n in ("fan","bearing","stepper","servo"):
        p=PH[n].cut(cutbox)
        if not p.vals(): continue
        T=tri(p); pc=Poly3DCollection(T,edgecolor="none",facecolor=(.3,.45,.9,.55)); ax.add_collection3d(pc)
    ax.set_xlim(-105,105); ax.set_ylim(-105,105); ax.set_zlim(240,450); ax.view_init(*view); ax.set_axis_off(); ax.set_title(title)
plt.tight_layout(); plt.savefig("out/head_section.png",dpi=85)
