import sys, numpy as np, matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt, importlib.util
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
spec=importlib.util.spec_from_file_location("ap","airraikordee_parts.py"); ap=importlib.util.module_from_spec(spec); spec.loader.exec_module(ap)
def mesh(s,tol=0.4):
    v,t=s.val().tessellate(tol,0.5); V=np.array([[p.x,p.y,p.z] for p in v]); return V,np.array(t)
def draw(ax,V,T,col,elev=28,azim=-60,lim=None):
    tri=V[T]; n=np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]); n/=np.linalg.norm(n,axis=1,keepdims=True)+1e-9
    sh=np.clip(n@np.array([.35,-.55,.75]),0,1)*.55+.4
    pc=Poly3DCollection(tri,edgecolor="none"); pc.set_facecolor(np.c_[np.outer(sh,col).clip(0,1),np.ones(len(sh))]); ax.add_collection3d(pc)
    if lim is None:
        mn,mx=V.min(0),V.max(0); c=(mn+mx)/2; r=(mx-mn).max()/2
    else: c,r=lim
    ax.set_xlim(c[0]-r,c[0]+r); ax.set_ylim(c[1]-r,c[1]+r); ax.set_zlim(c[2]-r,c[2]+r); ax.view_init(elev,azim); ax.set_axis_off()
names=sys.argv[1:] or list(ap.PARTS)
cols=4; rows=(len(names)+cols-1)//cols
fig=plt.figure(figsize=(4.2*cols,4.0*rows),facecolor="white")
for i,n in enumerate(names):
    s=ap.PARTS[n][0](); V,T=mesh(s)
    ax=fig.add_subplot(rows,cols,i+1,projection="3d")
    view={"03_base_shell":(25,-70),"11_head_body":(40,-120),"12_head_top":(-30,-60),"06_filter_wall_rear":(20,-120),"07_filter_door_front":(20,-60)}.get(n,(28,-60))
    draw(ax,V,T,np.array([.95,.62,.25]) if n[:2]>="11" else np.array([.45,.65,.68]),*view); ax.set_title(n,fontsize=10)
plt.tight_layout(); plt.savefig("out/parts_preview.png",dpi=80)
