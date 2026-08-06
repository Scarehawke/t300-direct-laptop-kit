# Prepared stock-T300 upgrade

> **Quarantined:** this document records an earlier local proposal. It is not
> an installation guide. `t300_core.cfg` is locally authored and does not meet
> the current community-evidence policy in `CHANGE_POLICY.md`. Do not run
> `install-core` and do not print G-code that requires this overlay.

This is the plain-language handoff for the offline package prepared on
2026-08-03. It assumes a stock Comgrow/Sovol T300 on vendor firmware 1.5.2,
Klipper 0.12.0, the stock inductive probe and bed hardware, and the already
installed purchased GerGo knob-leveling macro.

The owner installed and verified `t300_core.cfg` release candidate 3 on
2026-08-03. Later include-order analysis showed that its locally proposed
`idle_timeout`, `extruder`, and `resonance_tester` values were shadowed by later
factory sections. Its lifecycle macros were active. The live include was then
removed under the community-evidence policy. Factory lifecycle and power-loss
behavior are active again; the inert file remains only as rollback evidence.

## The main rule

GerGo remains the only gantry-leveling workflow. One small T300-specific file,
`t300_core.cfg`, becomes the single owner of print start, print end, pause,
resume, cancel, and filament changes. General-purpose macro suites are not
stacked on top of one another.

That matters because the factory software and community suites often redefine
the same command names. Combining them can make one macro save state while a
different macro tries to restore it.

## Tweaks prepared now

### Turn heat off after ten idle minutes

The factory changed Klipper's idle action to a message only. That could leave
the nozzle or bed heating forever after an abandoned manual heat command. The
overlay restores heater, fan, and motor shutdown after ten minutes with no
activity.

If a print has been paused for that long, restart it rather than trying to
resume after the motors have released.

### Restore extrusion safety limits

The factory permits an extrusion cross-section of 500 mm2 and extruder speeds
up to 2000 mm/s. Those values defeat Klipper's protection against malformed
XY-plus-E moves and very aggressive stationary E-only commands. The
cross-section setting does not itself limit a stationary extrusion.

The prepared limits are 1.0 mm2, 60 mm/s for extruder-only movement, and
3000 mm/s2 extruder-only acceleration. The new purge uses about 0.21 mm2, so it
fits with ample margin. The current 50 mm/s Orca retraction also still fits.

### Give startup one clear owner

The slicer now emits one line:

```gcode
START_PRINT BED_TEMP=65 EXTRUDER_TEMP=215 MESH=FULL
```

The printer macro then performs the complete sequence:

1. Validate the requested temperatures and mesh mode.
2. Start heating the bed and explicitly keep the nozzle heater off.
3. Wait for the bed, then home all axes.
4. Build a fresh full 9x9 mesh at the requested bed temperature.
5. Raise Z to 10 mm before moving in X or Y.
6. Move to X20/Y20, then heat the nozzle to the requested temperature.
7. Descend vertically and draw two purge lines while moving.
8. Raise to Z2 and hand control to Orca's first model move.

There is no stationary prime blob, no low-Z diagonal travel, and no second
slicer-side homing or heating sequence. Keeping the nozzle cold during the
81-point mesh avoids several minutes of dripping filament.

### Use a full mesh for the baseline

The prior saved mesh had about 0.659 mm of height range, which is large. A
fresh full mesh is slower, but it gives us a known baseline after the new
mechanical and probe calibration.

KAMP's useful claim is that it can probe only the part of the bed occupied by
the current objects and their margin. That saves time. It does not make the
probe more accurate or the bed flatter, and careless settings can sample fewer
points across a warped area. Adaptive meshing stays off until repeated full
meshes prove what point density this bed needs and the vendor `bed_mesh.py`
changes have been captured.

### Make pause, resume, cancel, and filament changes agree

The replacement commands share the same assumptions:

- move only when all axes still have a known position;
- lift only as far as the remaining Z space allows;
- extrude or retract only while Klipper says the nozzle is hot enough;
- use the real Klipper 0.12 filament-sensor field, `enabled`;
- refuse resume if motors released or filament is absent;
- turn heaters off even when cancel cannot safely park;
- make `M600` pause first instead of immediately pulling filament inside the
  model.

The public command names remain unchanged so the touchscreen and Orca can keep
using them.

### Disable unaudited power-loss resume

The factory recovery macro tells Klipper to pretend X, Y, and Z are all at zero
without homing, then runs shell scripts that were not present in the Moonraker
configuration backup. That can create confident movement from a false
position.

`RESUME_INTERRUPTED` is therefore quarantined and `force_move` is disabled.
The live installer requires an explicit acknowledgement of this change. The
feature can be reconsidered only after `/home/mks/plr.sh`,
`/home/mks/clear_plr.sh`, the screen button mapping, and a disposable recovery
test are all reviewed.

### Make resonance testing less violent

The factory sets `resonance_tester.accel_per_hz` to 200. The overlay returns it
to Klipper's documented 75 baseline. This changes only future resonance tests;
it does not invent new input-shaper values or alter normal print acceleration.

## Slicer corrections

The new normal PLA first-layer temperature is 215 C. The old 220 C value is
retained only as the hottest section of the temperature tower. The 65 C first
bed layer remains for now because the thin test sheet's need for a spatula does
not by itself prove excessive adhesion, and changing bed temperature would
also change the bed's shape during this Z test.

The official Orca T300 process quietly inherited 125% bottom-solid flow. The
diagnostic first-layer process and provisional Frieren process now override
that to 100%. This prevents extra plastic from hiding a high nozzle or creating
ridges after the new calibration.

The rebuilt center test proves, from its actual G-code:

```text
requested bead width:     0.5000 mm
inferred path spacing:    0.4575 mm
designed path overlap:    0.0425 mm
extrusion-area error:     0.11%
```

The model is supposed to become one fused sheet. Its prior uniform gaps were
not designed into the test.

Prepared files are currently in the ignored local build cache:

```text
.cache/prepared-gcode/calibration/T300_PLA_FIRST_LAYER_CENTER_215C.gcode
.cache/prepared-gcode/calibration/T300_PLA_FIRST_LAYER_FIVE_POINT_215C.gcode
.cache/prepared-gcode/calibration/T300_PLA_TEMP_STRING_220-195C.gcode
.cache/prepared-gcode/frieren/T300_FRIEREN_NATIVE_012_PROVISIONAL_DO_NOT_PRINT.gcode
```

Frieren is 35.22 g with an estimated time of 4h58m56s. It uses the safe startup
contract and 215 C, but it remains `DO_NOT_PRINT` until temperature, pressure
advance, overall flow, and retraction are accepted from shorter tests.

## Deliberately unchanged

These are measurement gates, not forgotten improvements:

- **Probe Z offset:** use the newly saved live calibration; never bake an
  offset correction into sliced G-code.
- **Mesh speed 400 mm/s:** compare probe repeatability and repeated meshes
  before lowering it.
- **Mesh fade:** measure the new live mesh range before deciding whether the
  factory fade settings distort Z.
- **Input shaper:** capture fresh ADXL345 data on the printer's real table.
- **Normal motion limits:** derive conservative values from quality and
  attended missed-step tests.
- **Axis-twist compensation:** measure repeatable X-axis probe bias first.
- **TMC autotune:** do not change driver registers without exact stock motor
  constants.
- **Automatic nozzle-contact Z calibration:** the stock inductive probe cannot
  perform it because there is no separate nozzle-contact switch.
- **Timelapse and notifications:** useful conveniences, but unrelated to the
  first-layer failure.

All researched community projects are pinned in
`third_party/community-sources.lock.json` and cached for offline review. Their
source is reference material unless this document explicitly says otherwise.

## The build-sheet limitation

No macro in this package can prove the removable metal sheet is installed. The
inductive probe detects that metal sheet; without it, a Z home can drive the
nozzle into the underlying plastic bed.

Before every command or file that can run `G28`, physically check:

1. the metal build sheet is installed and seated flat;
2. the nozzle tip is clean;
3. no print, tool, hand, or filament lump is in the motion path.

A real automatic interlock would require an independent sheet-presence sensor
and wiring. Software using the same missing-sheet-sensitive probe cannot solve
this by itself.

## Hookup sequence

Keep the owner beside the printer for every movement and heating step.

### 1. Read only

```bash
./bin/t300-link up
python3 ./bin/t300ctl.py discover
python3 ./bin/t300ctl.py check --host PRINTER_IP
python3 ./bin/t300ctl.py backup --host PRINTER_IP
python3 ./bin/audit-t300-config.py PATH_TO_NEW_BACKUP
```

Record the current software versions, saved probe offset, mesh range, included
files, filament-sensor state, temperatures, and whether the printer is idle.
Do not apply the old saved offset or mesh over the new calibration.

### 2. Private SSH capture

Use the owner's normal printer SSH login in Kitty to download, without editing:

- `/home/mks/plr.sh` and `/home/mks/clear_plr.sh`;
- the live vendor `bed_mesh.py` and its Klipper Git/version information;
- the stock serial-TFT bridge configuration and T300 button/action mapping;
- any service or script referenced by the recovery configuration.

Hash the downloaded files and keep them outside the public Git repository.
This is the only prepared research step that still needs the printer password.

### 3. Dry run

The following historical command is intentionally blocked by the tool:

```bash
python3 ./bin/t300ctl.py install-core --host PRINTER_IP
```

Review the compatibility result, macro list, exact one-line include diff, and
any update diff. A dry run uploads nothing and restarts nothing.

### 4. Install and verify configuration only

Do not perform this historical step. It is retained to explain the earlier
proposal and rollback records:

```bash
python3 ./bin/t300ctl.py install-core \
  --host PRINTER_IP \
  --apply \
  --acknowledge-plr-quarantine
```

The installer first creates a complete live backup. It restores the old macro
and `printer.cfg` automatically if Klipper does not become ready.

After restart, run only:

```text
T_CORE_STATUS
```

Confirm the expected version message. Query temperatures and printer state
again. Do not home yet.

### 5. First attended motion

Physically perform the three build-sheet checks above. Then run the rebuilt
center patch from the beginning. Do not apply a temporary Z raise or lower;
this test is evaluating the newly saved physical calibration.

The expected visible order is bed heating, homing, a full 9x9 probe grid, a
Z-first move to the purge location, nozzle heating, two moving purge lines, and
then the center patch. Stop for scraping, loose lines, a growing nozzle blob,
unexpected coordinates, power instability, or any missing-sheet doubt.

Let the patch cool. A correct result has touching/fused lines and peels as one
sheet. Fine line texture is normal; open air gaps are not.

## Historical offline evidence

- 45 Python unit tests cover config backup, include placement, compatibility
  rejection, safety ordering, and audit rules.
- Four process-level cases pass on exact upstream Klipper 0.12.0, including
  deliberate rejection of invalid mesh mode, unpaused resume, and factory
  power-loss resume.
- The original merged-config claim was invalid because the audit did not model
  textual include order correctly. The auditor has since been corrected.
- All four prepared G-code files pass command, temperature, motion-bound,
  acceleration, feed, and volumetric-flow audits.
- Both first-layer files additionally pass path-overlap and extrusion-math
  audits.

The remaining proof is deliberately physical: the private vendor scripts and
one attended center patch. Installing the overlay does not itself require a
new gantry, probe-offset, or saved-mesh calibration; its first print creates a
fresh full mesh as part of the attended startup sequence.
