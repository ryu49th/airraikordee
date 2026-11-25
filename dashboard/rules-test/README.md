# Database rules tests

Tests `../database.rules.json` with the Firebase Realtime Database emulator. Needs Node 18+ and Java 11+ (the emulator).

```
cd dashboard/rules-test
npm install
npm test
```

35 cases: owner reads and writes `config` (range and format checks), stranger and signed-out users denied, device account writes telemetry / env / state / history / cause / alerts / health in one multi-path PATCH, owner cannot write telemetry, `ui_cmd` create-only (device deletes after handling), `feedback` create-only, claim once, wrong or used code denied, `claims` and other users' `devices` cannot be listed, `deviceSecrets` write-only for the owner.
