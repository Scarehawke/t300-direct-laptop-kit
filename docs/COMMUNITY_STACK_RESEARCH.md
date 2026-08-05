# T300 community stack research

> **Policy update, 2026-08-03:** proposals in this document are research only.
> A locally assembled macro is not community approved merely because it uses
> ideas from community projects. Apply the evidence gate in `CHANGE_POLICY.md`.
>
> **Runtime update, 2026-08-04:** the installed KAMP-only subset did not alter
> factory extrusion limits. The separate, unapproved runtime proposal now sets
> only `max_extrude_cross_section: 5`, KAMP Line Purge's documented minimum.
> See `PRELIMINARY_IMPLEMENTATION_AUDIT_20260804.md`; do not combine decisions
> from these two generations of the design.

Research date: 2026-08-02

This document evaluates community Klipper macros and extensions for a stock
Comgrow/Sovol T300 running vendor firmware 1.5.2 and Klipper 0.12.0. The
installed GerGo Z-tilt-via-knob workflow is the one deliberate exception to
stock hardware. The original bed mounts, inductive probe, hotend, extruder,
motors, drivers, and removable build sheet are assumed.

This is a decision record, not an installation guide. No candidate should be
installed on the live printer until its configuration has passed the offline
Klipper 0.12.0 harness and an attended dry run.

## Executive decision

Do not install an all-in-one macro suite. The T300 already has overlapping
vendor overrides for print start/end, meshing, pause/resume/cancel, temperature
waits, filament changes, timelapse, and power-loss recovery. Adding another
suite creates rename chains and shared state that are difficult to reason about
and harder to recover when a print is active.

Build a small T300-specific layer with one owner for each command:

1. Keep GerGo as the only gantry-alignment implementation.
2. Preserve the vendor print-lifecycle macros unless a traceable, compatible
   T300 community implementation is selected and explicitly approved.
3. Use object-aware adaptive meshing only after a full-mesh baseline and a
   repeatability test prove it is appropriate for this unusually warped bed.
4. Use community tools primarily as focused diagnostics, preferably processing
   data on the laptop rather than modifying the vendor Klipper installation.
5. Pin every external revision. Never enable printer-side automatic updates.

## Immediate stock-config findings

These findings came from the complete T300 configuration backup captured before
the GerGo installation. They are more urgent than optional macro features.

### P0: idle timeout leaves heaters on

The factory `[idle_timeout]` replaces Klipper's safe default with only a
`RESPOND` message. Klipper 0.12.0 documents the default timeout action as
`TURN_OFF_HEATERS` followed by `M84`; defining custom `gcode` removes that
default. The package must restore heater shutdown. Motor shutdown can be timed
separately if preserving Z alignment is desirable.

### P0: extrusion guards are effectively disabled

For a 0.4 mm nozzle, Klipper's default `max_extrude_cross_section` is
`4 * nozzle_diameter^2`, or 0.64 mm2. The factory value is 500 mm2. Factory
extrude-only velocity and acceleration are also set to 2000 mm/s and
10000 mm/s2. These settings turn useful typo and malformed-G-code protection
into decoration.

The final purge must fit within a defensible extrusion limit. KAMP's stock line
purge asks users to raise the cross-section limit to 5 mm2, so it should not be
copied unchanged merely to replace the current wipe.

### P1: the mesh wrapper discards intent

The factory `BED_MESH_CALIBRATE` override:

- forces a 65 C bed regardless of the requested material temperature;
- ignores caller arguments, including its own `ADAPTIVE=1` argument;
- always invokes the renamed base command with only `ADAPTIVE=1`;
- depends on a vendor-patched feature that is absent from upstream Klipper
  0.12.0.

The live vendor `bed_mesh.py` must be inspected before deciding whether to use
that patch or replace it with a version-pinned macro implementation.

### P1: print lifecycle has multiple failure paths

The factory start macro fully heats the nozzle before an 81-point mesh, which
encourages ooze. Its delayed two-pass state machine makes slicer ordering
significant. The pause/cancel paths contain inconsistent filament-sensor field
names, missing parameter defaults, a misspelled homing field, and temperature
checks that do not consistently use `min_extrude_temp`.

This should be replaced as one coherent unit. Replacing only one of start,
pause, resume, cancel, or filament change would leave coupled state behind.

### P1: generic motion values are not calibration

The factory config permits 600 mm/s and 12000 mm/s2 and ships fixed input
shapers. Those are limits and factory guesses, not measurements of this
assembled printer on its current table. The built-in ADXL345 and
`resonance_tester` are valuable, but they first need an accelerometer query and
mount/orientation verification.

### P2: mesh fade deserves measurement

The factory mesh uses `fade_start: 0`, `fade_end: 10`, and `fade_target: 0`.
Klipper warns that fade on a significantly warped bed can shrink or stretch Z,
and recommends omitting `fade_target` when the mesh average is the desired
target. The previous recorded mesh range was about 0.659 mm, so fade behavior
must be tested rather than accepted by default.

## Candidate matrix

| Candidate | Real value | T300 decision |
| --- | --- | --- |
| KAMP | Object bounds from `exclude_object`, adaptive mesh, smart park, and adaptive purge | **Use Smart Park and Line Purge only.** The T300's native adaptive mesh completed successfully. Do not include KAMP's `Adaptive_Meshing.cfg`, which conflicts with the vendor wrapper and has a T300-specific failure report. The installed KAMP-only subset left factory limits unchanged; the later review-only runtime separately proposes KAMP's 5 mm2 minimum. |
| KAMP_LiTE | KAMP purge and park without KAMP meshing | **Reference only.** It supports the same native-mesh plus adaptive-park/purge architecture selected here, but the installed implementation stays pinned to the original KAMP project. |
| Mainsail config | Mature pause/resume/cancel parking, safe extrusion checks, layer pause controls | **Use as a behavioral reference or pinned component after integration tests.** It owns the same macro names as the vendor and does not preserve T300 recovery/touchscreen helpers by itself. |
| Demon Klipper Essentials Unified | Broad lifecycle, heat stability, mesh profiles, filament, homing, safety checks, legacy-Klipper detection | **Do not install wholesale.** It is actively maintained and has useful ideas, but its core overrides homing, M84, lifecycle, leveling, and recovery. That is too much ownership beside GerGo and the vendor screen. |
| Klippain | Full modular configuration, calibration workflows, adaptive mesh, ShakeTune integration | **Do not install wholesale.** It is a replacement configuration framework, not a conservative add-on for a frozen vendor image. |
| jschuh/klipper-macros | Comprehensive lifecycle, layer actions, surface offsets, heater scaling, adaptive mesh | **Do not install wholesale.** The maintainer explicitly warns against mixing override suites and expects current stock Klipper rather than old forks. |
| ShakeTune | Input-shaper graphs, axes mapping, mechanical diagnostics, vibration profiles | **Adopt laptop-side first.** The T300 already has an ADXL345. Capture data using stock Klipper and process legacy CSV on the laptop, avoiding a Python plugin inside the vendor image. |
| Ellis `TEST_SPEED` | Detects missed steps by comparing MCU positions before and after an attended motion pattern | **Optional diagnostic.** Use conservative values only after homing repeatability is known. It is not a license to use the discovered failure limit as a print setting. |
| Klipper Auto Speed | Binary-searches motion failure limits | **Reject for baseline.** Its maintainer says it is under development and validated only on CoreXY, while the T300 is a Cartesian bed slinger with sensorless XY homing. |
| TMC Autotune | Calculates driver registers from exact motor constants | **Defer.** The exact stock motor constants are not established, and the extension changes TMC behavior involved in sensorless homing. Wrong inputs can trade noise for heat or missed steps. |
| Automatic Z Calibration | Automatic nozzle-to-bed offset with a separate switch/probe workflow | **Incompatible with stock hardware.** The inductive probe alone cannot perform the required nozzle-contact measurement. |
| Native axis-twist compensation | Corrects probe bias caused by a twisted X rail and probe/nozzle offset | **Measure, then likely adopt if non-zero.** It exists in upstream Klipper 0.12.0 and directly fits this probe arrangement. Calibration must precede Z offset, GerGo alignment, and bed mesh. |
| Native skew correction | Software compensation after measuring a calibration object | **Do not enable now.** Reported dimensions are accurate. Measure XY diagonals and correct mechanically before adding software compensation. |
| Native pressure advance | Reduces corner bulging and non-print-move ooze | **Calibrate per filament.** The stock 0.02 value is generic. Tune extrusion and temperature first, then store PA in the Orca filament profile. |
| Native input shaper | Reduces ringing based on measured resonances | **Recalibrate.** Prefer stock `SHAPER_CALIBRATE` plus laptop analysis over a printer plugin at first. Validate smoothing as well as resonance suppression. |
| `PROBE_ACCURACY` | Measures probe repeatability and standard deviation | **Adopt as a diagnostic.** Run cold and at printing temperature before trusting denser or saved meshes. |
| Multi-temperature saved meshes | Avoids probing every print and accounts for temperature-dependent bed shape | **Experiment, not baseline.** First capture repeated full meshes at each temperature and quantify within-temperature repeatability versus between-temperature change. A stale mesh is worse than a slower fresh one. |
| Native screws-tilt adjust / silicone-level macros | Reports physical bed-screw adjustments | **Incompatible with the stock fixed spacers.** Reconsider only after an intentional adjustable-spacer hardware conversion. |
| Nozzle cleaning/brush macros | Repeatable physical wipe and purge | **Hardware-dependent.** No baseline support without a securely mounted brush and verified coordinates. |
| Moonraker timelapse | Print recording and inspection | **Retain as optional.** The bundled macro is 1.14 versus upstream 1.15; the only macro change is configurable frame-check timing. It does not improve print quality. |
| Vendor power-loss recovery | Can reconstruct and resume interrupted G-code | **Quarantine and audit.** It enables `force_move`, sets kinematic positions, and calls shell scripts that were not included in the captured config backup. Disable its callable resume path until the complete implementation and touchscreen contract are tested. |

## Proposed package boundaries

### Printer: `t300_safety`

- Restore heater shutdown on idle.
- Reduce extrusion limits to values justified by the hotend and purge design.
- Validate temperatures, homed axes, and move bounds before extrusion or park
  moves.
- Keep a single emergency/cancel path that turns heaters off even when motion
  state is unknown.
- Document that stock hardware cannot automatically prove the removable steel
  sheet is installed. A positive build-sheet interlock requires an independent
  sensor.

### Printer: `t300_lifecycle`

Own `START_PRINT`, `END_PRINT`, `PAUSE`, `RESUME`, `CANCEL_PRINT`, and filament
change as a set. Preserve the public names used by the touchscreen and Orca,
but remove hidden two-pass state.

The start sequence should be parameter driven:

1. Validate bed/nozzle targets and reset stale pause state.
2. Heat the bed while keeping the nozzle heater off to prevent mesh-time ooze.
3. Home with the nozzle clear of debris.
4. Apply the selected mesh policy at the actual target bed temperature.
5. Move vertically to clearance, then XY near the purge location.
6. Heat the nozzle to the requested first-layer temperature.
7. Descend vertically and purge only while XY is moving.
8. Hand control to sliced print moves.

No stationary prime blob and no combined low-Z XY travel should be present.

### Printer: `t300_mesh`

- Enable Moonraker object processing and require Orca object labels.
- Provide an explicit full-mesh fallback when no objects are available.
- Use a margin that includes skirts/brims and probe/nozzle offsets.
- Preserve or improve physical probe spacing; never equate fewer probes with
  greater accuracy.
- Probe at the requested bed temperature after a measured stabilization policy.
- Keep GerGo alignment separate because it requires human knob adjustment.
- Do not enable fuzz for the stock non-contact inductive probe.

The initial comparison should use the same first-layer model and filament for:

1. a fresh full 9x9 mesh;
2. an adaptive mesh preserving the 9x9 full-bed point spacing;
3. an adaptive mesh with a deliberately higher local minimum density.

Judge adhesion, line fusion, measured thickness, mesh time, and repeatability.

### Laptop: analysis and deployment

Extend `t300ctl.py` instead of installing general update managers on the
printer:

- audit exact Klipper/Moonraker builds, macro owners, duplicate sections, unsafe
  static limits, and the vendor adaptive-mesh patch;
- back up all config and required recovery scripts before every change;
- install with dry-run, an explicit file manifest, pinned source hashes, and
  automatic rollback;
- run candidate configs against exact Klipper 0.12.0;
- capture and compare mesh matrices across temperatures;
- collect resonance CSV files and invoke ShakeTune on the laptop;
- preserve third-party licenses and keep the purchased GerGo source private.

## Validation phases

### Phase 0: snapshot and safety

1. Fetch a new complete backup after the recent calibration.
2. Record live Z offset, mesh range, firmware build, and all included files.
3. Audit the full power-loss shell path before retaining PLR hooks.
4. Fix idle heater shutdown and extrusion guards first.
5. Confirm the removable plate is installed before every attended Z home.

### Phase 1: mechanical and probe baseline

1. Run GerGo gantry alignment.
2. Run `PROBE_ACCURACY` cold and at the PLA bed temperature in the center and
   representative corners.
3. Measure axis twist; enable compensation only if repeatable bias is present.
4. Recalibrate Z offset, then generate three full meshes at one temperature.
5. Compare those meshes to separate bed deformation from probe noise.

### Phase 2: lifecycle replacement

1. Test all macros in the offline Klipper 0.12.0 harness.
2. Dry-run start with heaters disabled, then with filament removed.
3. Validate start, pause, filament runout, resume, cancel, normal end, and idle
   timeout while attended.
4. Re-slice both the first-layer test and Frieren against the same macro
   contract. Keep Frieren provisional until the first-layer test passes.

### Phase 3: mesh policy

Run the full-versus-adaptive comparison above. Add temperature-indexed saved
meshes only if repeated data shows they predict bed shape better than a fresh
adaptive mesh.

### Phase 4: extrusion and motion

1. Calibrate extrusion/flow and PLA temperature in Orca.
2. Calibrate pressure advance for the actual spool.
3. Verify the ADXL345 attachment and axes, capture resonance data, and choose
   shapers with acceptable smoothing.
4. Establish conservative velocity/acceleration through attended tests and
   print quality, not by adopting the first missed-step threshold.

### Phase 5: optional services

Only after print fundamentals are stable: timelapse, notifications, saved mesh
selection, and audited power-loss recovery. None should be in the critical
motion path without a tested fallback.

## Licensing and source policy

This repository is MIT licensed. KAMP, Mainsail config, DKEU, Klippain,
jschuh/klipper-macros, ShakeTune, TMC Autotune, automatic Z calibration, and
Moonraker timelapse are GPL-3.0 projects. Their code must not be pasted into MIT
files. A future installer may fetch a pinned upstream revision into a clearly
separate component, or the T300 behavior may be implemented independently from
Klipper's documented interfaces.

The purchased GerGo package stays private. This public repository may automate
verification and private installation from the owner's archive, but must not
contain, quote, or redistribute its source.

## Reviewed revisions and primary sources

- [Klipper v0.12.0](https://github.com/Klipper3d/klipper/tree/v0.12.0), commit
  `0d67d9c45d2dc39f8b4be7d1bb54b94b2698a2b6`
- [KAMP](https://github.com/kyleisah/Klipper-Adaptive-Meshing-Purging), commit
  `b0dad8ec9ee31cb644b94e39d4b8a8fb9d6c9ba0`
- [KAMP_LiTE](https://github.com/3DPrintDemon/KAMP_LiTE), commit
  `9288cdb220bd1fb39db57035ac751e8091ce9b78`
- [Mainsail config](https://github.com/mainsail-crew/mainsail-config), commit
  `ff3869a621db17ce3ef660adbbd3fa321995ac42`
- [Demon Klipper Essentials Unified](https://github.com/3DPrintDemon/Demon_Klipper_Essentials_Unified),
  commit `1b1a8b8068f5617893965bfbe50e8d76530fdc6a`
- [Klippain](https://github.com/Frix-x/klippain)
- [jschuh/klipper-macros](https://github.com/jschuh/klipper-macros), commit
  `ebae0a3b6ec4bf7096e7b068967b283992078f5f`
- [ShakeTune](https://github.com/Frix-x/klippain-shaketune), commit
  `354ee4be6db218715239f7b9411c2c37fdea23dd`
- [Ellis Print Tuning Guide](https://github.com/AndrewEllis93/Print-Tuning-Guide)
- [Klipper Auto Speed](https://github.com/Anonoei/klipper_auto_speed), commit
  `63315317c465edc135e4712e4327de5b8b852082`
- [Klipper TMC Autotune](https://github.com/andrewmcgr/klipper_tmc_autotune),
  commit `3d1ab9f106910604a046b4140b3755935cfaa0c9`
- [Automatic Z Calibration](https://github.com/protoloft/klipper_z_calibration),
  commit `374d487feabe738e910b1b1d2f68ceaf3742a235`
- [Moonraker timelapse](https://github.com/mainsail-crew/moonraker-timelapse),
  commit `c7fff11e542b95e0e15b8bb1443cea8159ac0274`
- [Official T300 firmware notes](https://wiki.sovol3d.com/en/T300)
- [Klipper bed mesh documentation](https://www.klipper3d.org/Bed_Mesh.html)
- [Klipper axis twist compensation](https://www.klipper3d.org/Axis_Twist_Compensation.html)
- [Klipper resonance measurement](https://www.klipper3d.org/Measuring_Resonances.html)
- [Klipper pressure advance](https://www.klipper3d.org/Pressure_Advance.html)
