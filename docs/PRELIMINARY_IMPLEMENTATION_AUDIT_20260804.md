# Preliminary T300 implementation audit

Audit date: 2026-08-04

Status: **offline review only**. The printer was powered off and was not
contacted. No configuration was uploaded, no service was restarted, and no
movement or heating command was sent. The runtime installer remains hard
quarantined until the owner approves the design.

## Plain-language verdict

The reported failures are real, but they do not all have one cause. The old
startup had several independent extrusion paths, the factory failure controls
are internally inconsistent, and the long Frieren strings are a filament
profile problem rather than a bed-mesh or purge-macro problem. The powerline
dropout remains unproven and should not be “fixed” by changing printer motion.

The preliminary package now gives one component ownership of each print
lifecycle command. It adopts pinned community code for pause/resume/cancel and
adaptive purge, then adds only the T300-specific heat, vendor-mesh, touchscreen,
and high-cleaning-park glue those projects cannot know about.

## Problem-to-change map

| Observed or audited problem | Evidence that it is real | Preliminary response | Stringing effect |
| --- | --- | --- | --- |
| Ball forms before the purge moves | Old KAMP setting advanced 3.5 mm while stationary; old Orca starts also extruded at fixed XY | Pinned KAMP Line Purge with upstream `tip_distance: 0`, 20 mm object margin, and no other positive stationary extrusion | Removes the known commanded cause; passive heat-up ooze still needs the moving purge |
| Purge crowds the model/supports | Failed Frieren photos and the 10 mm object-only margin | Increase KAMP's supported `purge_margin` setting to 20 mm | None after the purge |
| Cancel can error or appear delayed | Factory typo `homed_axe`, inverted/unhomed branches, and whole-template expansion | Pinned Mainsail cancel performs retract/shutdown/native cancel; a delayed T300 hook parks afterward. This fixes the broken path, not G-code queue latency | Final retract can reduce a cancellation tail |
| Cancel did not finish at cleaning height | Factory cancel and `END_PRINT` were separate paths | Shared guarded exit: rise by 10 mm or to Z 200, whichever is higher; cap at max Z; never move down | None during the model |
| Idle preheat can remain hot | Factory `idle_timeout` replaced shutdown with a message | Normal idle turns all heaters and the part fan off after 10 minutes while leaving motors enabled | None |
| Runout pauses twice | `pause_on_runout` already pauses, then factory G-code calls `M600` | Keep the automatic pause; post-runout action only reports the problem | Prevents state loss, not strings |
| Load/unload can leave bad coordinate state or extrude during an active print | Factory helpers change modes before checking temperature and do not guard print state | Require Pause during a print, refuse cold extrusion, and wrap E moves in `SAVE_GCODE_STATE`/`RESTORE_GCODE_STATE` | Cleaner filament handling only |
| Power-loss resume can move from invented coordinates | Factory path uses `SET_KINEMATIC_POSITION` and uncaptured scripts | Disable `force_move` and replace resume with a clear refusal | None |
| A malformed moving-extrusion command can request an enormous bead | Factory cross-section allowance is 500 mm2 | Set only the KAMP-documented 5 mm2 requirement; preserve all other factory E/motion limits | Catches gross XY-plus-E errors, not stationary extrusion or normal strings |
| Apply/rollback can accept stale state, interrupt a newly started print, or leave files behind | Old tool trusted upload response and an immediate stale `ready` response; Ctrl-C bypassed normal exception handling | Compare before write, read back bytes, check idle before upload and restart, restore without restart if a print appeared, roll back Ctrl-C, delete new files, require restart transition then ready | None |
| Timelapse can waste storage | `saveframes: True` stores a ZIP in addition to the rendered video | Separate proposal for `saveframes: False`, verified first on a short render | None |
| Camera evidence disappears on restart | crowsnest has `delete_log: true` | Conditional `false` patch only after log rotation or a size cap is confirmed | None |
| Preview becomes choppy or disappears with recording | Browser, timelapse snapshots, and FFmpeg can be simultaneous camera clients | Use layer timelapse alone for normal prints; leave FPS/resolution unchanged until hardware formats are captured | None |
| Powerline link disappeared during one Frieren attempt | The link returned after cancellation, but one temporal correlation does not identify heater noise, PLC placement, mixed adapters, camera load, or a printer service | No printer change. Test in order: idle, heaters, layer timelapse, then optional second stream; keep PLC units directly in wall outlets and record link state | None |
| Frieren strings heavily between islands | Photos show repeated travel strings; source used provisional temperature/retraction and suppressed some infill retractions | Keep an editable project, disable suppression, inherit the eventual calibrated retraction, and do Orca calibrations before slicing | This is the actual model-wide stringing work |
| Missing steel sheet allows a nozzle collision | The inductive probe failed to see the underlying plastic bed | Mandatory physical sheet check; software using the same probe cannot create a real interlock | None |

## Community code versus local glue

**Pinned community components**

- Mainsail `client.cfg` owns `PAUSE`, `RESUME`, and `CANCEL_PRINT` at revision
  `ff3869a621db17ce3ef660adbbd3fa321995ac42`.
- KAMP owns only `SMART_PARK` and `LINE_PURGE` at revision
  `b0dad8ec9ee31cb644b94e39d4b8a8fb9d6c9ba0`.
- The vendor T300 adaptive mesh remains the only mesh owner.
- Moonraker timelapse remains in documented layer-macro, non-parking mode.
- Orca's own calibration order is the basis for filament tuning.

**Locally authored integration**

- `START_PRINT` validates two temperatures, uses a 150 C nozzle standby, calls
  the vendor's fixed-65 C mesh, restores the requested bed target, smart-parks,
  finishes nozzle heating, and runs the moving purge.
- `END_PRINT` and the delayed post-cancel hook share a bounded exit helper.
- Touchscreen filament command names route into guarded Mainsail/runtime paths.
- The deployment transaction and exact-known-start 3MF migrator are local
  tooling. They are tested, but still require owner review and an attended live
  acceptance run. The migrator validates a temporary archive before publishing
  a final filename, so failed validation leaves no plausible-looking output.
- The generic leveling installer now refuses either GerGo or the open gantry
  workflow when the competing include is already selected.

## Side effects and unresolved choices

1. KAMP's `tip_distance: 0` removes the blind stationary filament push that
   produced the observed ball. It cannot prevent passive heat-up ooze. After a
   large prior retract, the first part of its 30 mm moving purge may also be
   sparse. That is an accepted test tradeoff; KAMP is a purge line, not a
   physical nozzle brush. Its object margin also cannot see every support,
   brim, or skirt, so the first attended preview remains mandatory.
2. The vendor mesh always probes at 65 C and ignores caller bounds/counts. The
   runtime compensates for the final requested bed target, but does not pretend
   this wrapper is generic. Capture the patched vendor `bed_mesh.py` before
   changing mesh ownership.
3. After a one-hour pause, the proposal turns off the hotend and fan but leaves
   the bed and motors active so the part does not release. This preserves a
   resumable print but leaves the bed energized; it is an explicit owner-review
   decision, not a hidden safety claim.
4. `clear_last_file` is a vendor command. It runs only after heater shutdown and
   parking, so a failure cannot keep the print active, but its implementation
   and its interaction with timelapse autorender still belong in the next
   read-only appliance capture.
5. The crowsnest log patch matches upstream's default, but preserving an
   unlimited verbose log is not robust. It stays conditional until retention is
   bounded.
6. Both editable 3MFs contain stale “Generic PETG” and “T500” default-profile
   labels even though their active embedded material/printer settings are T300
   PLA. Do not use those labels to choose a preset. Reconcile them in Orca when
   the calibrated PLA profiles are saved.
7. Ordinary Cancel cannot preempt a G-code command already holding Klipper's
   queue, such as homing, probing, or a temperature wait. The maintained macro
   repairs what happens when Cancel executes; Emergency Stop remains the only
   immediate response to a mechanical or thermal hazard.
8. The high cleaning park assumes Orca's normal `by layer` sequence. The
   migrator now refuses sequential `by object` projects because a completed
   tall object can obstruct a later object's cancel path.
9. Setting Orca's `reduce_infill_retraction` to `0` makes it retract on more
   infill travels. That can reduce the observed island-to-island strings, but
   increases retraction count and some print time; use it only with the eventual
   calibrated retraction profile.
10. `saveframes: False` keeps the rendered timelapse and removes the redundant
    frame archive. Individual source frames will no longer be available after
    a successful render, which is why the change waits for a short test.

## Held factory findings

The staged whole-stack audit has no P0 finding, but it still reports six P1 and
five P2 items. They are held because suspicious is not the same as proven:

- duplicate probe speed definitions, effective value 5 mm/s;
- vendor mesh wrapper discarding parameters;
- resonance excitation at 200 mm/s2/Hz;
- factory E-only ceilings of 2000 mm/s, 10000 mm/s2, and 10 mm/s instantaneous;
- mesh travel at 400 mm/s and fade behavior on a strongly warped bed;
- very low saved input-shaper damping values;
- nonstandard factory `M109` and `M190` wrappers.

Do not run resonance testing until the machine/table and accelerometer are
checked. Do not replace factory limits with generic Klipper defaults without
T300-specific evidence. Do not edit mesh points by hand.

## Editable project state

- Frieren:
  `.cache/prepared-gcode/frieren-runtime-preliminary-20260804/`
  `T300_FRIEREN_RUNTIME_TIMELAPSE_PRELIMINARY_DO_NOT_PRINT.3mf`
  (`759f906f23bbd550678af3b1b0f3c592cce6384ff480abef0c5e429a18be0177`)
- Camera mount:
  `.cache/prepared-gcode/camera-mount-runtime-preliminary-20260804/`
  `T300_CAMERA_MOUNT_RUNTIME_ORANGE_PRELIMINARY_DO_NOT_PRINT.3mf`
  (`a605193f814dff710a5b2430c3ff323dd74f57b24604dc241c39fa438dd18ac4`)

Both use the runtime start/end contract and one non-parking timelapse frame per
layer. Every embedded model/metadata member other than Orca's project-settings
JSON was verified byte-for-byte unchanged. Neither has final G-code.

The orange mount still has provisional 210 C, flow 1.0, maximum volumetric
speed 12 mm3/s, and disabled pressure advance. Frieren still has 220 C first
layer, 215 C later layers, flow 1.0, maximum volumetric speed 15 mm3/s, and
disabled pressure advance. These facts are reasons to calibrate, not values to
silently “correct.”

## Next live acceptance gates

1. Take a fresh read-only backup and compare hashes with the proposal input.
2. Capture the vendor mesh extension, PLR scripts, service definitions,
   crowsnest formats/log retention, free storage, and touchscreen mappings.
3. Review and approve each runtime and service decision separately.
4. With an empty bed and the steel sheet physically present, install only the
   approved runtime transaction and verify restart state. The owner initiates
   every motion, heat, and calibration command.
5. Test fresh-boot start, normal end, pause/resume, runout, and cancel using a
   disposable short print. Verify heater shutdown before judging the park, and
   verify the physical Emergency Stop separately while cold with a clear bed.
6. Test a short timelapse render with `saveframes: False`; do not run the second
   continuous recorder.
7. Calibrate each actual PLA spool in Orca: temperature, max volumetric speed,
   pressure advance, flow, then retraction. Dry the spool/check the nozzle if a
   retraction tower strings at every setting.
8. Reopen the two editable projects with the calibrated profiles, inspect
   supports and preview, then slice and run the camera mount before Frieren.

## Offline validation result

- 31-file source backup checksum verification: pass.
- Python and shell syntax checks: pass.
- Repository unit tests: 103 passed.
- Exact upstream Klipper 0.12 process harness: 11 cases passed at commit
  `0d67d9c45d2d`.
- Staged complete include-tree audit: 0 P0, 6 P1 held, 5 P2 held, 8 info.
- Runtime review-bundle `SHA256SUMS`: all 10 public artifacts pass.
- Frieren and camera-mount 3MF ZIP integrity: pass; non-settings members were
  verified unchanged during migration.

The current generated bundle is
`.cache/prepared-runtime-final-audit-20260804-v4/`. Its private staging tree is
gitignored and contains the owner's purchased macro only to compile the full
include graph; that private source is absent from the public payload.

## Primary references

- [Mainsail maintained client macros](https://github.com/mainsail-crew/mainsail-config/blob/master/client.cfg)
- [KAMP](https://github.com/kyleisah/Klipper-Adaptive-Meshing-Purging)
- [Klipper command templates](https://www.klipper3d.org/Command_Templates.html)
- [Moonraker server state](https://moonraker.readthedocs.io/en/latest/external_api/server/)
- [Moonraker timelapse](https://github.com/mainsail-crew/moonraker-timelapse/blob/main/docs/configuration.md)
- [Crowsnest configuration](https://crowsnest.mainsail.xyz/configuration/crowsnest-section)
- [OrcaSlicer calibration](https://github.com/OrcaSlicer/OrcaSlicer/wiki/Calibration)
- [TP-Link powerline electrical-interference guidance](https://www.tp-link.com/ca/support/faq/882/)
