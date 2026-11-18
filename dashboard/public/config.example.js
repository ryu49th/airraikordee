// Copy this file to config.js (same folder) and fill it from
// Firebase console → Project settings → Your apps → Web app → SDK setup and configuration.
// The web apiKey is not a secret (see the data contract), but config.js stays out of Git so each
// person can point at their own project. Without config.js the page runs in demo mode.
// The values below are placeholders: copy the real ones from the console, especially databaseURL.
export const firebaseConfig = {
  apiKey: "",
  authDomain: "airraikordee.firebaseapp.com",
  databaseURL: "https://airraikordee-default-rtdb.asia-southeast1.firebasedatabase.app",
  projectId: "airraikordee",
  appId: "",
};

// Optional, for testing on your own computer only: sign in automatically with a test owner account
// (one that has already claimed a unit), so the page opens straight to the data.
// Use a throwaway test account, never your real password. config.js is git-ignored; keep it that way.
// export const devLogin = { email: "test-owner@example.com", password: "..." };
