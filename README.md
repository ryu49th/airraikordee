# AirRaiKorDee

Portable smart air purifier with a rotating head (camera + outlet) and a smart charging box.
CEDT Embedded Systems final project, due 8 Dec 2026.

| Folder | What | Lead |
|---|---|---|
| `cad/` | printable parts (CadQuery) + fit check | |
| `node1/` | ESP32-S3 firmware in the unit | |
| `node2/` | charging box firmware (XIAO ESP32C3) | |
| `gateway/` | Jetson Nano software | |
| `dashboard/` | web dashboard (Firebase) | |
| `docs/` | protocol, diagrams, test results, report | |

Shared contract between all parts: `docs/protocol.md`. Change it only in a PR the whole team sees.

## Rules
- Never push to `main`: branch (`node1/pms5003`, `gateway/uart-bridge`, ...) → pull request → one teammate approves.
- Never commit secrets: use `secrets.example.h` / `.env.example`, the real files are git-ignored.
- Commit messages start with the folder: `node2: add INA226 alert ISR`.
