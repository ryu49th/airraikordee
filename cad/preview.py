import importlib.util, numpy as np, matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
spec=importlib.util.spec_from_file_location("tc","test_coupons.py"); tc=importlib.util.module_from_spec(spec); spec.loader.exec_module(tc)
names=list(tc.PARTS)
fig=plt.figure(figsize=(15,7.5),facecolor="white")
for i,n in enumerate(names):
    s=tc.PARTS[n]().val(); v,t=s.tessellate(0.15,0.3)
    V=np.array([[p.x,p.y,p.z] for p in v]); T=np.array(t)
    ax=fig.add_subplot(2,5,i+1,projection="3d")
    pc=Poly3DCollection(V[T],facecolor="#2A9D8F",edgecolor="none",alpha=1)
    # simple shading
    nrm=np.cross(V[T][:,1]-V[T][:,0],V[T][:,2]-V[T][:,0]); nrm/=np.linalg.norm(nrm,axis=1,keepdims=True)+1e-9
    sh=np.clip(nrm@np.array([.4,-.5,.75]),0,1)*.6+.35
    pc.set_facecolor(np.c_[0.16*sh/0.95+0.05,0.62*sh,0.56*sh,np.ones(len(sh))].clip(0,1))
    ax.add_collection3d(pc)
    mn,mx=V.min(0),V.max(0); c=(mn+mx)/2; r=(mx-mn).max()/2
    ax.set_xlim(c[0]-r,c[0]+r); ax.set_ylim(c[1]-r,c[1]+r); ax.set_zlim(c[2]-r,c[2]+r)
    ax.view_init(32,-58); ax.set_axis_off(); ax.set_title(n.replace("_"," "),fontsize=10)
plt.tight_layout(); plt.savefig("out/preview.png",dpi=110)
