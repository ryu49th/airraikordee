# AirRaiKorDee — project context for Claude Code

(Formerly "AirBuddy Go+" / "HaiJaiDee"; older files may still use those names.)

Embedded Systems final project (CEDT, Chulalongkorn), due 8 Dec 2026.
Portable smart air purifier with a rotating head (camera + air outlet) and a smart charging box (Node 2) on a DC cable.
Requirement summary: 2 sensor nodes + 1 gateway, >=5 sensor types, >=1 actuator, AI closed loop,
cloud + dashboard, notifications, safety rules, Git history from every member.

## System architecture

| Part | Hardware | Sensors | Role |
|---|---|---|---|
| Gateway (inside base) | Jetson Nano (JetPack 4), USB webcam on head, USB speaker | Camera, BME280 | Person + dust-cause detection, speech in/out, humidity compensation of PM, Firebase + Discord |
| Node 1 (inside unit) | ESP32-S3 DevKitC-1 N16R8 | PMS5003, INMP441 mic, stop button, NTC on the battery pack | On-device wake word (E7), fan PWM, pan stepper + tilt servo, UART to Jetson |
| Node 2 (charging box, beside the unit) | XIAO ESP32C3 | INA226 (ALERT pin -> ISR), DS18B20 (charger temperature) | Detects the cable is plugged into the unit (current), powers the unit, charges the battery only when allowed + temp/current OK |

Actuators: ARCTIC P14 Pro PST fan (140 mm, PWM), **28BYJ-48 stepper + ULN2003 for head pan** (replaces MG996R:
a 180° servo through the ≈14:1 ring gear would only turn the head ≈12°, and servos buzz while holding),
MG90S louver tilt servo. Pan homing: limit micro-switch on the ring (ISR, E2), position by step counting.
Filter: third-party Xiaomi Air Purifier 4 Compact filter (Shopee, ~฿289), Φ175 × 171 mm, HEPA + carbon, ~0.4 kg.
It is closed on ONE end (solid cap with a pull handle and 4 rubber feet) and open on the other:
install handle-end DOWN, open end UP toward the fan. Measure the real centre-hole diameter before printing seats.
Grade (H13) is not stated by the seller, so measure CADR ourselves.
Battery: 3S2P 18650 Li-ion, 6 genuine cells of one model and batch (LG MJ1 3500 mAh preferred, ≈78 Wh), spot-welded
by a battery shop (never soldered), 3S 20 A BMS, XT60 lead. Pack sits in a LiPo fire-safe bag or on a thin metal tray
(PETG burns). Separate buck: 5 V 5 A for Jetson, ~6 V 3 A for servos.
Depth items: E1 FreeRTOS, E2 interrupts, E4 ACK/retry + store-and-forward, E5 TLS + no secrets in Git, E7 on-device AI.

## Software specs

- Jetson Nano gateway: see `JETSON_GATEWAY_SPEC.md` (modules, MQTT/UART protocol, build order).

### Node 2 (charging box) firmware — charging rules (safety rule 4.3: never charge lithium unattended)

Hardware chain: 19 V adapter (มอก.) → DC jack → MOSFET (D4184, XIAO GPIO) → XL4015 CC/CV set to **12.6 V / 3 A**
→ INA226 (shunt + ALERT) → DC cable (1 m) → panel jack on the unit → ideal diode/BMS → pack.
DS18B20 in the box watches the XL4015 (> 60 °C → off). **Battery temperature = NTC taped to the pack, read by Node 1**,
sent via the Jetson (`ab/node1/telemetry.batt_temp_c`) and used by the box (> 45 °C → off).
Use a DIFFERENT barrel size on the unit (e.g. 5.5 × 2.5) than the 19 V adapter (5.5 × 2.1), so nobody can plug
the raw adapter straight into the unit and bypass the box.
The XL4015 does NOT terminate a charge (it holds 12.6 V forever), so the XIAO must.

Modes (one FreeRTOS state machine; report `mode` in every `ab/node2/telemetry`):
| Mode | Enter when | MOSFET | Leave when |
|---|---|---|---|
| `IDLE` | cable not plugged into a unit | off; every 2 s pulse on 0.2 s to probe | probe current > 50 mA → `DOCKED` |
| `DOCKED` (= plugged) | unit present, not allowed to charge | off (probe pulse every 2 s to confirm still docked) | `charge_allowed:true` from Jetson → `CHARGING`; probe 0 mA → `IDLE` |
| `CHARGING` | allowed = docked AND person seen ≤ 15 min (or app confirm) AND temp < 45 °C | on | any stop condition below |
| `FULL` | V ≥ 12.55 V AND I < 150 mA for 60 s | off | unplugged → `IDLE`; V < 12.0 V and allowed → `CHARGING` |
| `FAULT` | over-temp, over-current, or timer | off, latched | button/app reset after cause is gone |
| `FIELD_TEST` | config flag `field_test=true` AND probe shows **no battery** (V at pads follows the supply, current = load only) | on continuously (supplies the unit, nothing is charging) | flag off, or a battery is detected → `DOCKED` |

Stop conditions in `CHARGING` (any → MOSFET off within the stated time):
- INA226 ALERT (over-current > 3.5 A) → ISR → MOSFET off immediately (E2). → `FAULT`
- Battery NTC > 45 °C or box DS18B20 > 60 °C → off within 1 s. → `FAULT`
- Charge timer > 4 h → off. → `FAULT` (protects against a stuck "not full" reading)
- No `charge_allowed` refresh from the Jetson for 60 s (Wi-Fi/MQTT lost) → off. → `DOCKED`
- `charge_allowed:false` (nobody seen for 15 min, night rule, app) → off. → `DOCKED`
- Full (above) → `FULL`, Discord "ชาร์จเต็มแล้ว".

**24-hour field test (rule 4.3):** unplug the battery XT60 on the base tray, set `field_test=true`, and the box
just powers the unit from the certified adapter — no lithium is charging, so it may run unattended. Measure battery
life separately: charge while attended, unplug the cable, run until the BMS/low-voltage cut, log with the unit's telemetry.
State this method in the report as the way the team complied with 4.3.

## Mechanical design (all mm) — for Fusion 360

Overall: cylindrical tower Ø200, ≈384 tall (base 85 + filter halves 174 + top seat 10 + ring 20 + head 92 + top plate 3), standing directly on the floor/table;
the charging box sits beside it on a 1 m DC cable.
Interactive 3D reference of this layout: the "AirRaiKorDee 3D" artifact.
**Printable CAD (source of truth for dimensions): `cad/airraikordee_parts.py` (CadQuery, all sizes in `P`, bought-part sizes marked MEASURE)**
→ `cad/out/parts/*.stl|.step`, `cad/out/airraikordee_assembly.step`; `cad/check_fit.py` checks overlaps incl. head rotation + gear mesh. Print in PETG, wall 2.5,
assembly clearance 0.25, M3 heat-set inserts, every printed part must fit a 220 × 220 print bed.

Bottom to top:
1. **Charging box (Node 2)** 110 × 72 × 36, printed in 2 parts (body + bottom plate, 4 screws; all boards mount on the
   plate). Adapter jack on one side, cable gland on the other, status LED window on the top. Inside: XIAO ESP32C3,
   INA226, D4184 MOSFET, XL4015 CC/CV, DS18B20 touching the XL4015. Vent slots over the XL4015.
   **Plug detection:** every ~2 s the XIAO switches the MOSFET on for 0.2 s; if the INA226 sees current, the cable is
   plugged into a unit (`plugged:true`). Unplugging drops the current to 0 instantly.
   (The earlier round dock with pogo pins was dropped: a cable is simpler and always makes contact.)
2. **Base** Ø200 × 85, hollow. Holds Jetson Nano (100 × 80 × 29 + heatsink, keep ≥10 clearance and vent slots),
   battery pack (~70 × 55 × 40, measure real pack), buck converters, ESP32-S3.
   Front: small intake grille (≈40 × 20) for PMS5003 (50 × 38 × 21) + BME280 — far from the outlet.
   Front: Ø16 stop-button hole. **Right wall (+X): panel DC jack 5.5 × 2.5 for the charging cable**, ≈22 above the floor.
   Bottom: 4 rubber feet. NTC thermistor taped to the battery pack (2-pin plug to the ESP32-S3).
3. **Filter section** Ø195 × 174 (+ 10 mm top seat), 36 vertical slots (360° intake, ≈600 cm² open area), split in two halves:
   - **Front half = hinged door** (18 slots): hinge pin on the left (−X) side, magnet + pull tab on the right (+X).
   - **Rear half = fixed wall** (18 slots), screwed to the base, with a wire channel from base to head.
   - **Each half is ONE printed part**: 18 vertical ribs (7 wide × 3 thick, ~10 mm slots) joined by
     three horizontal bands (bottom, middle, top; 8 high × 4 thick). Print standing upright, bottom band on
     the bed: footprint ≈195 × 100, height 180, no supports (slots are vertical; band tops are short bridges).
   - Hinge: 4 alternating print-in-place knuckles (2 on door, 2 on wall) + Ø3 steel rod or long M3 screw as pin.
     Magnets: 6 × 3 mm neodymium pressed into pockets top and bottom on the door's right edge and the wall.
   - **Fixed top and bottom seats**, no moving parts. Soft foam strip 8–10 mm on both seats, and a
     **chamfered front lead-in** on each seat so the filter compresses the foam by itself as it slides in.
     If the seal leaks in testing, add 3 compression springs under the bottom seat (fallback).
   - Filter change: open door → slide filter out forward (door opening 195 > filter 175)
     → push new filter in until it touches the rear wall (handle end down) → close door → reset filter baseline in the app.
   Air enters radially and rises through the filter centre.
4. **Pan ring** Ø200 × 20. Seat for a **94 × 94 mm lazy-susan bearing** (bought part: centre hole Ø54,
   Φ4 mounting holes, plates rotated 45° to each other; measured: 10 thick, hole pitch 82) low in the ring (z 5–15). Its
   corners reach r≈66 — keep the pinion (inner edge r≈61) above the bearing, never at the same height.
   The bearing hangs on a thin **4-spoke cross** (fixed plate → ring; rotating plate → head), NOT on a solid plate,
   so air flows AROUND it as well as through the Ø54 hole (open area ≈ 138 + 23 cm² > fan ≈ 120 cm²).
   A printed **liner sleeve** clips into the Ø54 hole so the harness never rubs on the sheet-metal edge.
   The ring also carries an **internal ring gear**
   (module 2, **89 teeth**, pitch Ø178, ring z 14–20) fixed to the ring. Ring Z: spokes 0–5 (on the axes, they carry the
   bearing's bottom plate; the rear spoke also covers the harness groove), bearing 5–15, gear 14–20. The bearing is 10 tall overall but its plates are thin sheet: the bottom plate (fixed) sits at z 5–6.5, the TOP plate turns with the head, so the pinion never passes a bearing corner (the 1 mm overlap in height with the gear is harmless).
   Bearing top plate holes sit on the diagonals → 4 separate printed spacers (20, Ø10 × 5) between the head floor arms and the top plate, M3 from below into inserts (head underside stays flat for printing). Limit switch sits on the rear spoke (r 78). **Limit switch** at the rear (180°); a tab
   (21, screwed under the head front with 2 × M3 from below) hits it at ±180° → homing at boot + hard stop.
5. **Head** Ø200 × 92 + 3 mm top plate, printed as **11a head_lower (z 0–33: floor, bearing posts, stepper, fan, camera window) + 11b head_upper (z 33–92: outlet, louver shelf, top-plate bosses)**, joined by 4 × M3 at 32/148/205/335°, so the pan drive can be built and tested before the upper part. **Camera = swappable cassette in a 64 × 24 front window** (2 screws from outside): 19a for a bare camera board (lens hole + 4 M2 standoffs; fits boards ≤ ~26 mm tall), 19b for a cheap webcam (big lens hole + shelf with zip-tie slots). **Cylinder with a flat top** (no dome — a vertical front wall takes a clean outlet
   cut-out and prints easier; top plate printed separately and screwed on). Was Ø170: a 140 mm fan is ≈198 mm
   across the diagonal. Fan frame corners rounded r14 to clear the 2.5 mm wall. Two decks:
   - **Lower deck (y 0–35):** 140 × 140 × 25 fan (Thermalright TL-C14CW, measured; screw holes 124.5) lying flat in the centre, blowing up.
     The 4 pockets between the square fan and the round wall each hold one thing:
     - front (+Z): **USB camera**, lens through a hole in the front wall, directly BELOW the outlet, so it
       looks where the air goes;
     - left (−X): **USB speaker**, grille in the left wall;
     - right (+X): **28BYJ-48 stepper** (Ø28 × 19, shaft down, shaft 8 mm off-centre — point it toward the
       axis; its blue wire box sits on the OPPOSITE side, i.e. pointing OUTWARD, and the 5 wires leave the box's outer end) in a pocket shaped for the real motor (ears 42 × 7, wire box 17 × 4 + 3 mm wire gap open to the top), slotted −0.5…+1.5 mm radially to set gear mesh (further inward the can touches the fan), held down by a printed **strap over the motor top** (18_stepper_strap, 2 screws into posts) — NOT by its ear screws: heads under the floor would sit at r 87 and hit the ring-gear teeth; **12T module-2 pinion (Ø24)** at r = 77 meshes the
       internal ring gear (89T, Ø178) → 7.42:1, head ≈2 rpm (180° ≈15 s). The 28 mm motor does not fit the 27.5 mm gap
       between fan and wall, so the head wall bulges out locally at +X (to r ≈ 105, 40 wide, z 0–28) around the motor and its wire box. Pinion is mounted hub-down under the floor. Turns with the head. De-energise coils
       when idle (gearbox holds). ULN2003 board sits on the base tray next to the ESP32-S3;
     - rear (−Z): harness entry from the pigtail, second disconnect.
   - **Upper deck (y 35–95):** air plenum.
     - Rear half: a **curved deflector** (quarter-dome, r≈97, 60 high) turns the fan's upward flow forward.
     - Front: **outlet cut-out in the front wall, ±48.6° (150 mm chord) × 55 high (y 38–93), ≈82 cm²**
       (the old 84 × 40 = 34 cm² choked the ≈120 cm² fan). Louver frame recessed in the opening (the frame
       fills the whole opening width — there is NO room beside it for a servo).
     - **Vane mechanism (car-AC-vent parallelogram):** 4 horizontal vanes, each on a Ø3 steel pivot pin
       (axis X) through holes in BOTH frame side walls. Each vane has a **crank tab 11 mm long** pointing
       backward near its right end (x≈55). One vertical **tie bar** pins all 4 tabs together, so all vanes
       always have the same angle.
     - **MG90S** lies on its side BEHIND the louver, right side, shaft parallel to the vane axes (X), horn also
       11 mm long at the same x as the tabs. A straight **push rod** (≈26 mm) joins the horn tip to the tie bar.
       Horn length = tab length → servo angle = vane angle 1:1. Range ±30°, servo stops on timeout like the pan.
     - **Finger guard = a 140 mm fan grill screwed on top of the fan** (openings ≤ 6 mm, safety rule 4.3),
       not behind the louver — that space is needed for the tie bar and push rod.
     - **Dry bay** between the deflector and the rear wall/top: **USB hub** and **INMP441 mic** (mic hole in
       the top plate, rear) — out of the airflow and away from fan noise. Cables enter it through a slot at the
       deflector rim.

## Wiring (design the cable path into every printed part)

Goal: as few wires as possible cross the rotating joint. Only ONE harness goes from base to head:
- **1× USB 2.0 cable** Jetson → a **mini USB hub in the head** (rear, above the fan) → webcam + USB speaker.
- **~14 single silicone-insulated wires, 26–28 AWG, bundled** (NOT a stiff PVC multicore — silicone survives
  repeated twisting): 12 V (fan), 6 V (servos), 3.3 V (mic), GND ×2, fan PWM, fan tach,
  4 stepper coil wires + stepper 5 V, tilt servo signal, I2S SCK / WS / SD (INMP441 → ESP32-S3). Keep I2S wires twisted with GND.
  Use a thin, soft USB cable.
Route (bottom → top):
1. **Base:** Jetson USB + ESP32-S3 header wires meet at a **disconnect point on the base top, rear** (JST-XH or
   a 12-pin connector) so the filter section can be lifted off.
2. **Filter section:** a covered **vertical cable channel in the rear wall at 180°** (≈20 × 10 mm, between the
   filter Ø175 and the slots), notched through both seats OUTSIDE the foam ring so the seal is not broken.
3. **Cross the rotating joint ON THE AXIS** (cable only twists, it is never dragged around the rim):
   - A printed **spoke** on top of the top seat carries the harness from the rear channel to the centre.
   - In the pan-ring opening the harness forms a **loose coiled pigtail, ~3 turns, r≈18 (coil OD ≈ 44, fits the Ø54 bearing hole), ~30 cm long**, around
     the axis. ±180° pan just tightens/loosens the coil by half a turn. The coil sits above the top seat, so it
     never blocks filter removal, and it lies in the dead-air zone under the fan hub.
   - Clamp both ends firmly so all bending happens in the coil, never at a connector.
   - Pan limited to **±180°** in firmware (track cumulative step count, always unwind the same way, never spin
     continuously) AND by the **limit switch** on the ring (safety rule 4.3).
   - Endurance test for the report: 2,000 cycles −180° → +180° (~3 h) while the Jetson checks the USB camera is
     still enumerated and the mic still streams each cycle.
4. **Head:** the pigtail ends under the fan hub, runs out along one fan strut to the rear pocket (second
   disconnect), then up through the deflector-rim slot into the dry bay (hub, mic). From there:
   hub → back down → around the LEFT side of the fan → speaker (left pocket) and camera (front pocket);
   power/signal → around the RIGHT side → stepper + fan, and up to the MG90S. Clip wires to the head wall
   (8 printed zip-tie loops at 20/65/115/160/195/235/305/345°); the fan corners leave only ~4 mm to the wall, so wires pass
   UNDER each corner in a 12 × 3 floor groove (45/135/225/315°). Nothing may reach the fan blades.
Inside the base: battery → 10 A fuse → switch → ideal diode → bucks (18 AWG on the 12 V / 5 V power path,
20–22 AWG servo power, 26–28 AWG signals). Box → unit is power only (one DC cable); Node 2 data goes over Wi-Fi.
Add strain relief (zip-tie anchors printed into the parts) at both disconnect points and at the ring trough exit.

## Serviceability (every part replaceable with one tool, no soldering inside the unit)

Rules for every printed part and harness:
- **One tool:** all joints use M3 screws into heat-set inserts, one 2.5 mm hex key. No glue on anything that may need service.
- **Plugs, not solder, between modules:** every wire that crosses a module boundary ends in a keyed, labelled
  connector (JST-XH / JST-PH, XT60 for battery, 4-pin fan, 5-pin stepper, 3-pin servo). Label both ends.
- **Clips and slotted holes** for bought parts (camera, speaker, hub, stepper) so they come out without
  removing anything else; slotted stepper plate also sets gear mesh.
Module-by-module access:
| Module | How to open | What you reach |
|---|---|---|
| Filter | front door (magnet), no tools | filter, foam strips; door lifts off its hinge pin |
| Head top | 4 screws; the curved deflector is part of the top plate | USB hub, mic (dry bay), then fan + grill (4 screws) |
| Head pockets | after fan is out (camera: from OUTSIDE, 2 screws) | camera cassette 19a/19b (front), speaker (clip, left), stepper (strap, 2 screws, right) |
| Louver | 2 screws from the front → pull the **louver cassette** (frame + vanes + tie bar + MG90S) out of the opening | whole tilt mechanism, one 3-pin plug |
| Whole head | unplug harness in rear pocket → 3 screws → lift off the bearing | lazy susan, ring gear, pigtail |
| Pan ring | 3 screws to the filter wall | limit switch (2 screws, 2-pin plug) |
| Base | **bottom plate = electronics tray**, 4 screws, slides down | Jetson, ESP32-S3 (socketed on headers), ULN2003, bucks, ideal diode; battery pack in its own holder on XT60 |
| Base front | **front sensor module** (grille + PMS5003 + BME280 + stop button on one plate), 2 screws from outside | the PMS5003 fan wears — swap in a minute |
| Base rear | **rubber service flap**, no tools | Jetson USB/LAN, ESP32-S3 USB-C, microSD, blade fuse |
| Charging box | flip it, 4 screws on the bottom plate (all boards mounted on it) | XIAO, INA226, MOSFET, XL4015, DS18B20, LED; cable unplugs at both ends |
Wear parts to keep spares of: filter, foam strip, head harness (pigtail), DC cable, PMS5003.
Software side: OTA for both ESP32s (E6 bonus), `git pull` + systemd restart on the Jetson over SSH, and health
telemetry that flags filter clogging (fan RPM), PMS5003 hours, stepper homing failures, battery cycles.

## Instructions for Claude when using the Fusion 360 MCP

1. Create Fusion **user parameters** for every dimension above (so the team can resize after measuring real parts).
2. Build each part as its own **component**, in the order listed, stacked on the Z axis.
3. Start with simple placeholder bodies for bought parts (filter, fan, Jetson, battery, servos, PMS5003) so fit can be checked.
4. Shell printed parts to 2.5 mm; add fillets ≥2 mm on outer edges.
5. Add revolute joints for the pan ring and the louver so rotation can be checked for collisions.
6. Split parts that exceed 220 × 220 × 220 and add alignment pins / screw bosses.
7. Save often; ask before deleting or overwriting bodies.
