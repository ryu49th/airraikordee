# Dashboard

Static web page + Firebase Realtime Database + Auth. No build step: plain HTML, CSS and ES modules,
Firebase JS SDK 10.12.2 loaded from gstatic.

Paths and field names follow Farn's Firebase data contract (13 Nov 2026). The dashboard reads
`devices/(deviceId)/…` and writes only `config`, `feedback` and the claim (`users/(uid)/devices`, `claims/(code)`).

## Run it

**Demo mode (no Firebase):** serve `public/` and open it. Without `public/config.js` (or with `?demo=1`)
the page makes up a unit with 7 days of history and streams new values every second.

```bash
cd dashboard/public && python3 -m http.server 8000     # then open http://localhost:8000
```

Opening `index.html` straight from Finder does not work (ES modules need http).

**Real project:** copy `public/config.example.js` to `public/config.js` and paste the web app config from
Firebase console → Project settings → Your apps. `config.js` is git-ignored. Sign in with an owner account,
then claim a unit with the code on its sticker.

**Skip the login while testing:** after one owner account has claimed the unit, add
`export const devLogin = { email: "...", password: "..." };` to your own `public/config.js` (git-ignored) and
the page signs in by itself. Use a throwaway test account. Login cannot be removed for real: the database
rules only let a signed-in owner read a unit (E5).

**Deploy (DB-12):** `npm i -g firebase-tools`, `firebase login`, then from `dashboard/`:
`firebase deploy --only hosting --project airraikordee`. Make sure `public/config.js` exists first.

## Screens

| Tab | Reads | Writes |
|---|---|---|
| ภาพรวม (AIR-50, 54) | `telemetry/node1/latest`, `env/latest`, `state`, `cause/latest`, `telemetry/node2/latest` | `feedback/{push}` = `{cause_ts, correct, true_cause}` |
| ประวัติ (AIR-51) | `history/node1` with `limitToLast(60 / 1440 / 10080)` | |
| ที่ชาร์จ (AIR-53) | `telemetry/node2/latest` | |
| แจ้งเตือน (AIR-55) | `alerts`, last 50 | |
| สถานะระบบ (AIR-56) | `health`, `config.filter_reset_ts` | |
| ตั้งค่า (AIR-57) | `config` | `config`: `pm_alert`, `fan_mode`, `manual_fan_pct`, `night_start`, `night_end`, `filter_reset_ts` |

The whole unit shows as offline when `telemetry/node1/latest.ts` is older than 90 s (`health` has no `ts`
in the contract).

## Files

- `public/index.html`, `public/style.css`: all screens, light and dark theme, works from 360 px wide.
- `public/js/app.js`: sign-in, claim, subscriptions, rendering.
- `public/js/logic.js`: pure helpers (PM2.5 bands, offline check, config validation, history series). Tested.
- `public/js/store.js`: the Firebase back end and the in-memory demo back end behind one small API.
- `public/js/demo.js`: fake data for demo mode. `public/js/chart.js`: small SVG line chart, no library.

Tests: `node --test dashboard/test/*.test.mjs` (Node 18+).

## Not here yet

- **Database rules**: they live in Farn's card AIR-49 and must match what is published, so this folder has
  no `database.rules.json` yet. Add it here once Farn publishes them.
- **Uploader side**: the gateway `uploader.py` (branch `gateway/bridge-uploader`) does not yet read `config`
  or `feedback`, and writes `state.alerts` as type strings; the dashboard accepts both strings and objects.
- **7 days**: `limitToLast(10080)` downloads ~10k rows. If it is slow, add the hourly history the contract
  mentions and point the 7-day view at it.
- LINE linking (A-09…A-12) and buttons that need `ui_cmd` (only in PR #3, not in the contract).

## Buttons that reach the unit (ui_cmd)
"หันมาหาฉัน" (overview), "หาสาเหตุตอนนี้" (dust cause), "ฉันอยู่ในห้อง ชาร์จได้" and "รีเซ็ตกล่องชาร์จ" (charging),
"ส่งข้อความทดสอบ" (Discord, settings) each push `{ts, by, cmd}` to `devices/(id)/ui_cmd`. The Jetson's
config_listener publishes it on MQTT and deletes the entry, so the page shows "เครื่องรับคำสั่งแล้ว" when the entry is
gone, or a timeout note after 20 s. In demo mode a fake unit deletes it after 1.5 s.

