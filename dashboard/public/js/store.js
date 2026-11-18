// Two back ends with the same small API:
//   auth:  onAuth(cb), signIn(email, pw), signUp(email, pw), signOut()
//   data:  listen(path, cb) / listenLast(path, n, cb) -> unsubscribe,
//          get(path), set(path, value), update(path, obj), push(path, value)
// firebaseStore talks to the real project; demoStore keeps a fake tree in memory.

const SDK = "https://www.gstatic.com/firebasejs/10.12.2/";

export async function firebaseStore(config) {
  const [{ initializeApp }, A, D] = await Promise.all([
    import(SDK + "firebase-app.js"),
    import(SDK + "firebase-auth.js"),
    import(SDK + "firebase-database.js"),
  ]);
  const app = initializeApp(config);
  const auth = A.getAuth(app);
  const db = D.getDatabase(app);
  const r = (path) => D.ref(db, path);

  return {
    demo: false,
    onAuth: (cb) => A.onAuthStateChanged(auth, (u) => cb(u ? { uid: u.uid, email: u.email } : null)),
    signIn: (email, pw) => A.signInWithEmailAndPassword(auth, email, pw),
    signUp: (email, pw) => A.createUserWithEmailAndPassword(auth, email, pw),
    signOut: () => A.signOut(auth),
    listen: (path, cb, onError) => D.onValue(r(path), (s) => cb(s.val()), onError),
    listenLast: (path, n, cb, onError) =>
      D.onValue(D.query(r(path), D.limitToLast(n)), (s) => cb(s.val()), onError),
    get: async (path) => (await D.get(r(path))).val(),
    set: (path, value) => D.set(r(path), value),
    update: (path, obj) => D.update(r(path), obj),
    push: (path, value) => D.push(r(path), value),
  };
}

// ---------------------------------------------------------------- demo

function walk(tree, path, create) {
  let node = tree;
  for (const part of path.split("/").filter(Boolean)) {
    if (node[part] === undefined || node[part] === null || typeof node[part] !== "object") {
      if (!create) return undefined;
      node[part] = {};
    }
    node = node[part];
  }
  return node;
}

function readPath(tree, path) {
  const parts = path.split("/").filter(Boolean);
  let node = tree;
  for (const p of parts) {
    if (node === null || typeof node !== "object" || !(p in node)) return null;
    node = node[p];
  }
  return node === undefined ? null : node;
}

function writePath(tree, path, value) {
  const parts = path.split("/").filter(Boolean);
  const key = parts.pop();
  const parent = walk(tree, parts.join("/"), true);
  if (value === null) delete parent[key];
  else parent[key] = value;
}

const clone = (v) => (v === null || v === undefined ? null : JSON.parse(JSON.stringify(v)));

export function demoStore(seed) {
  const tree = seed.tree;
  const listeners = new Set();
  let pushN = 0;

  function emit(l) {
    let v = readPath(tree, l.path);
    if (l.last && v && typeof v === "object") {
      const keys = Object.keys(v).sort().slice(-l.last);
      const out = {};
      for (const k of keys) out[k] = v[k];
      v = out;
    }
    l.cb(clone(v));
  }

  function changed(path) {
    for (const l of listeners) {
      if (path.startsWith(l.path) || l.path.startsWith(path)) emit(l);
    }
  }

  function add(path, cb, last) {
    const l = { path, cb, last };
    listeners.add(l);
    setTimeout(() => emit(l), 0);
    return () => listeners.delete(l);
  }

  const user = { uid: "demo-user", email: "demo@airraikordee.example" };
  const store = {
    demo: true,
    onAuth: (cb) => setTimeout(() => cb(user), 0),
    signIn: async () => user,
    signUp: async () => user,
    signOut: async () => alert("โหมดตัวอย่าง: ไม่มีการออกจากระบบจริง"),
    listen: (path, cb) => add(path, cb, 0),
    listenLast: (path, n, cb) => add(path, cb, n),
    get: async (path) => clone(readPath(tree, path)),
    set: async (path, value) => {
      writePath(tree, path, clone(value));
      changed(path);
    },
    update: async (path, obj) => {
      for (const k of Object.keys(obj)) writePath(tree, path + "/" + k, clone(obj[k]));
      changed(path);
    },
    push: async (path, value) => {
      const key = "-demo" + String(Date.now()) + String(pushN++).padStart(3, "0");
      writePath(tree, path + "/" + key, clone(value));
      changed(path);
      return { key };
    },
    // used by demo.js to stream fake telemetry
    _write: (path, value) => {
      writePath(tree, path, value);
      changed(path);
    },
  };
  return store;
}
