# Preliminary T300 runtime design

Design date: 2026-08-04

Status: **local proposal only**. Nothing in this document or its companion
files is approved for upload. The printer must remain unchanged until the
owner reviews each decision and a fresh read-only capture confirms the live
state.

## Goal

Replace the fragile print-lifecycle path with a small, reviewable stack while
leaving stock hardware tuning and calibration ownership alone. Every proposed
change below is tied to a reproduced defect or a documented dependency.

## Ownership model

Load these files last, in this order:

1. `mainsail_client.cfg`, generated from pinned upstream Mainsail
   `client.cfg`, owns `PAUSE`, `RESUME`, and `CANCEL_PRINT`.
2. `t300_runtime.cfg`, authored for this project, owns T300 integration,
   `START_PRINT`, `END_PRINT`, touchscreen filament helpers, idle shutdown,
   and the quarantine for power-loss resume.

The already approved KAMP subset continues to own only `SMART_PARK` and
`LINE_PURGE`. The vendor wrapper continues to own adaptive bed meshing. The
purchased GerGo package remains the only gantry-adjustment workflow.

## Proposed changes

### 1. Maintained pause, resume, and cancel

**Problem:** The factory macros require an undocumented `STATE` argument,
invert the hot-enough check, use incomplete homing guards, and contain a typo
that can make cancel fail while the printer is already in trouble.

**Proposal:** Generate an exact, checksum-verified copy of Mainsail's
maintained `client.cfg`, changing only its virtual-SD path from
`~/printer_data/gcodes` to the T300's existing `~/gcode_files`. Configure its
documented `_CLIENT_VARIABLE` interface in the T300 overlay.

**Why this boundary:** Pause/resume/cancel are one state machine. Installing
only a typo correction would leave the other failure paths intact.

### 2. One parameterized start command

**Problem:** The factory start macro is a hidden two-pass state machine. The
stock Orca profile also contains a stationary 25 mm extrusion. The previously
installed KAMP setting added another stationary 3.5 mm extrusion after final
heating. These paths explain the startup ball and corner-line behavior.

**Proposal:** Require `BED_TEMP` and `EXTRUDER_TEMP`, preheat the nozzle only
to 150 C while the bed, homing, and vendor mesh complete, call `SMART_PARK`,
finish heating, and then call upstream `LINE_PURGE`. KAMP stays at its current
upstream default `tip_distance: 0` and uses a 20 mm object margin.

The vendor mesh macro always probes at 65 C and does not restore a nonzero
previous target. The proposal therefore calls the vendor's existing bounded
`M190` wait again after meshing. This restores the slicer's requested bed target
and, unlike native Klipper `M190`, also waits when the bed must cool from 65 C.

There is no hand-authored purge geometry and no positive-E stationary move.
The slicer start is one line:

```text
START_PRINT BED_TEMP=[bed_temperature_initial_layer_single] EXTRUDER_TEMP=[nozzle_temperature_initial_layer]
```

### 3. Predictable completion and cancellation

**Problem:** Normal completion and manual cancellation have different and
partly broken cleanup paths. The requested cleaning position also needs to
respect tall prints.

**Proposal:** Retract 1 mm only when the extruder can extrude, stop heaters and
the part-cooling fan, then move to `X10 Y290` and to the greater of current
Z plus 10 mm or Z 200 mm. Bound Z by the configured maximum, never lower an
already higher toolhead, and move only when all axes are homed. Motors stay
enabled so Z cannot drop while the nozzle is being cleaned. A separate
`T_RELEASE_MOTORS` command is blocked during printing or pause. Runtime helper
names likewise contain no digits. Klipper 0.12's legacy parser truncates an
extended command when a run of digits is followed by more letters, so names
such as `T300_RELEASE_MOTORS` and `_T300_SAFE_EXIT` would be parsed as unrelated
commands. Messages also avoid semicolons because Klipper strips everything
after a semicolon as a comment even when it appears inside quotes.

Mainsail owns the actual cancel transaction. It retracts if hot, turns heaters
and the part fan off, clears pause state, and calls Klipper's native cancel.
The T300 hook only schedules a delayed cleanup. That delayed command runs after
the native cancel has been queued, so an unhomed axis or failed optional park
cannot prevent cancellation and heater shutdown.

This repairs the factory cancel path, but it does not turn ordinary Cancel into
an emergency stop. Klipper serializes G-code, so a soft cancel may wait behind
an in-progress home, probe, or temperature wait. Use the printer's Emergency
Stop for an immediate mechanical or thermal hazard; it intentionally abandons
position and resumability.

### 4. Heater idle safety without forced motor release

**Problem:** The custom factory idle macro replaced Klipper's default safety
actions with a message, so a valid manual preheat can remain on indefinitely.

**Proposal:** At the existing 600 second normal-idle timeout, turn off heaters
and the part-cooling fan. When a print is paused, use Mainsail's documented
one-hour pause timeout. If that expires, turn off only the hotend and fan, mark
Mainsail's `idle_state`, and keep the bed hot so the part does not release;
`RESUME` reheats the saved hotend target. Do not call `M84` in either branch,
which preserves the owner's requested separation between thermal shutdown and
motor release. A paused bed therefore remains hot and still requires normal
attended-printer judgment.

### 5. One filament-runout pause

**Problem:** `pause_on_runout: True` already invokes `PAUSE`, after which the
factory `runout_gcode` invokes `M600` and pauses a second time.

**Proposal:** Keep Klipper's automatic pause and replace post-runout G-code
with an error message only. Manual `M600` uses Mainsail's documented
`PAUSE X=10 Y=290 Z_MIN=50` pattern and does not automatically unload.
The touchscreen-compatible load/unload helpers refuse to move filament while
`print_stats` still says `printing`; pause first. They also check hotend state
before changing E and preserve the caller's coordinate/extrusion modes.

### 6. Quarantine unverified power-loss positioning

**Problem:** The factory recovery macro declares unmeasured X, Y, and Z
coordinates and then runs scripts absent from the configuration backup.

**Proposal:** Set `enable_force_move: False` and replace
`RESUME_INTERRUPTED` with a clear error. This can be revisited only after the
vendor scripts and complete recovery contract are captured and tested.

### 7. Narrow KAMP extrusion allowance

**Problem:** The factory permits `max_extrude_cross_section: 500`, while
Klipper's normal default for a 0.4 mm nozzle is about 0.64 mm2. KAMP explicitly
requires 5 mm2 for Line Purge.

**Proposal:** Set only `max_extrude_cross_section: 5`, exactly KAMP's documented
minimum. Do not change extruder velocity, acceleration, instantaneous corner
velocity, or any motor current. This guard applies to combined XY-plus-E moves;
it does not stop a stationary E-only push. Removing every stationary positive-E
command from startup is the separate fix for the observed ball.

### 8. Transactional deployment tooling

**Problem:** The current uploader can leave a newly created macro behind,
does not verify uploaded bytes, does not detect a live file changing between
review and apply, and records backup checksums with paths that do not match the
backup layout.

**Proposal:** Add compare-before-write checks, read-back verification,
rollback after every mutation failure, deletion of newly created files during
rollback, and backup verification. Runtime installation itself remains
disabled until owner approval. Recheck print state immediately before the first
write and again immediately before restart, after rereading every proposed
file. If a print starts during upload, restore the disk files without restarting
because Klipper has not loaded them. After `FIRMWARE_RESTART`, require Moonraker to report an observed
disconnect/startup transition and then `ready`; a stale pre-restart `ready`
response is not accepted. Klipper reuses its host process during a firmware
restart, so process ID is deliberately not used as proof. Ctrl-C after the
first mutation also enters rollback instead of abandoning staged files.

### 9. Timelapse storage and camera diagnostics

**Problem:** Layer timelapse currently stores temporary frames, a rendered
video, and a second ZIP of the frames. Continuous laptop recording also opens
another MJPEG client, which can make preview and a marginal powerline link less
stable. Crowsnest deletes its previous verbose log at every restart.

**Proposal:** Keep Moonraker timelapse in layer-macro mode with
`parkhead: false`, so one snapshot is taken per layer without added head
motion. Set `saveframes: False` after a short render test; this keeps the video
but avoids the extra ZIP. Do not run the continuous laptop recorder during a
normal timelapse print. Preserving crowsnest logs is only a conditional patch:
confirm log rotation or add a size cap first. Camera resolution and frame rate
remain unchanged until `v4l2-ctl` reports the camera's actual supported modes.

The one observed powerline dropout is not enough to blame timelapse or print
supports. TP-Link documents that appliance noise can make PLC links
intermittent and recommends direct wall outlets plus one-variable-at-a-time
testing. The acceptance run therefore records connectivity at idle, with
heaters, with layer timelapse, and only then with a second camera client. It
does not alter printing to conceal a network fault.

### 10. Slicer projects and stringing

**Problem:** Existing editable 3MFs contain several generations of startup
G-code. The camera-mount project still embeds Comgrow's two hard-coded purge
lines. Frieren also exposed model-wide travel stringing that lifecycle macros
cannot tune away.

**Proposal:** The migration helper accepts only byte-exact known legacy starts,
replaces them with the one-line runtime call, and verifies every non-settings
3MF member unchanged. Both migrated projects remain explicitly
`DO_NOT_PRINT`. Temperature, maximum volumetric speed, pressure advance, flow,
and retraction must be calibrated in Orca's documented order before Frieren is
resliced. The runtime purge can fix the initial blob, and the final retract can
reduce an end tail; neither is a general stringing calibration.

The migrator also requires Orca's `by layer` sequence. With sequential
`by object` printing, a completed object could stand above the current Z during
a later object's cancel, and a generic X/Y cleaning park cannot route around
that unseen obstacle safely.

## Explicit non-goals

- No movement, heating, homing, probing, or printer restart during preparation.
- No change to axis speed, acceleration, current, sensorless homing, heater
  verification, PID, pressure advance, input shaping, probe offset, mesh
  density, mesh fade, or GerGo behavior.
- No automatic missing-sheet claim; the stock inductive probe cannot prove the
  removable steel sheet is present.
- No KAMP adaptive-mesh override; the vendor adaptive implementation remains.
- No automatic powerline, crowsnest, Moonraker authorization, or OS update.
  The crowsnest log patch remains conditional. These need a fresh read-only
  system capture and, for the network dropout, evidence that the printer rather
  than the powerline adapter failed.
- No Frieren approval. Startup cleanup can prevent a starting blob and the end
  retract can reduce a final tail, but model-wide stringing still needs a
  filament-specific temperature/retraction test before another long print.

## Acceptance gates

Before this can become an installable release:

1. Generate a proposal from a fresh live backup and compare every input hash.
2. Pass repository unit tests and the exact upstream Klipper 0.12.0 harness.
3. Confirm no active macro has two lifecycle owners.
4. Confirm start contains no stationary positive extrusion and cancel always
   reaches heater shutdown without requiring optional parameters.
5. Review the diff and rollback plan with the owner.
6. Perform the first live restart with an empty bed and no print queued.
7. Test status-only commands, then pause/resume/cancel on a disposable print.
   Verify the physical Emergency Stop separately with heaters cold and the bed
   clear; do not expect soft Cancel to preempt an active home or probe.
8. Verify Moonraker renders a short timelapse with `saveframes: False` before a
   long print; verify bounded crowsnest log retention before preserving logs.
9. Capture camera formats and test the powerline link in order: idle, heaters,
   layer timelapse, then optional second stream.
10. Run Orca's filament calibrations before generating camera-bracket or
    Frieren G-code. The editable migrations are not print approvals.

## Primary references

- [Klipper command templates](https://www.klipper3d.org/Command_Templates.html)
- [Klipper configuration reference](https://www.klipper3d.org/Config_Reference.html)
- [Klipper G-code reference](https://www.klipper3d.org/G-Codes.html)
- [Mainsail maintained client macros](https://github.com/mainsail-crew/mainsail-config/blob/master/client.cfg)
- [KAMP configuration and purge documentation](https://github.com/kyleisah/Klipper-Adaptive-Meshing-Purging)
- [Moonraker file-manager API](https://moonraker.readthedocs.io/en/latest/external_api/file_manager/)
- [Moonraker server-state API](https://moonraker.readthedocs.io/en/latest/external_api/server/)
- [Moonraker timelapse configuration](https://github.com/mainsail-crew/moonraker-timelapse/blob/main/docs/configuration.md)
- [Crowsnest configuration](https://crowsnest.mainsail.xyz/configuration/crowsnest-section)
- [OrcaSlicer calibration guide](https://github.com/OrcaSlicer/OrcaSlicer/wiki/Calibration)
- [TP-Link powerline electrical-interference guidance](https://www.tp-link.com/ca/support/faq/882/)
