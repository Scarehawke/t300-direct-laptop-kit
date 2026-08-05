# Frieren failed-print audit

Date: 2026-08-03

This note records the failed first Frieren attempt and the offline corrections
prepared afterward. The printer was powered off while this work was done.

## What the photos establish

- The adaptive purge line formed a loose blob before its moving stroke.
- The purge line was too close to the skirt/support footprint.
- The early support and model islands produced extensive strings during travel.
- The powerline network disappeared during the print and returned when the
  printer was stopped. This is a network/power-environment problem, not evidence
  that support generation itself consumes unusual printer power.

## Purge diagnosis

The installed KAMP subset had `tip_distance: 3.5`. KAMP executes that value as
stationary extrusion immediately before the moving purge line. The T300 factory
`END_PRINT` retracts 4 mm, so 3.5 mm follows KAMP's tuning advice only when the
previous print ended normally. It is wrong after many cancels, failed starts,
and filament operations because the actual filament-tip position is unknown.

The prepared KAMP update therefore uses:

- `tip_distance: 0`: no blind stationary extrusion. A normally completed print
  may leave the first few millimeters of the 30 mm moving purge sparse, which is
  safer than producing a detached blob.
- `purge_margin: 20`: twice upstream KAMP's 10 mm default. Twenty millimeters is
  also the KAMP LiTE default and leaves more room for skirts and supports that
  can extend beyond the slicer's object polygon.

The rest of upstream KAMP `SMART_PARK` and `LINE_PURGE` is unchanged. The more
complex `DRAW_PURGE_LINE` from jschuh/klipper-macros does prime while moving, but
it depends on that project's larger state/options framework. Copying only that
macro would create an unreviewed local implementation, so it was not installed.

## Stringing diagnosis

The stopped print does not prove one single cause. Its G-code did combine three
conditions that can make stringing worse:

- the active filament profile used 0.5 mm retraction;
- Orca's `reduce_infill_retraction` option suppressed some travel retractions;
- the model printed at 215 C after its first layer, before this spool had a
  temperature or retraction calibration.

Moist filament can also produce stringing at every temperature. It must remain
on the list until a temperature tower is inspected.

An editable review copy now inherits the T300 machine profile's 0.8 mm
retraction and disables `reduce_infill_retraction`. The sliced G-code contains
more retractions, but Orca still omits some retractions for travel it classifies
as internal/safe. This is not yet a final Frieren file.

## Prepared diagnostics

`T300_PLA_TEMP_STRING_220-195C.gcode` is a 35-minute tower with six 10-degree
sections, bottom to top: 220, 215, 210, 205, 200, and 195 C. It uses the same
adaptive mesh, smart park, and line purge startup, but has no timelapse commands.
Its exact temperature sequence has been audited.

Read the tower by choosing the lowest-temperature section that still has sound
layer bonding and clean overhangs, then compare how much stringing remains. If
all six sections string badly, dry the spool before changing more slicer values.

After selecting temperature, use OrcaSlicer's built-in Retraction Test for a
direct-drive range of 0.0 to 2.0 mm in 0.1 mm steps. Choose the shortest section
that is clean. Do not guess a larger retraction from the failed figurine alone.

## Powerline isolation

Support geometry and the non-parking timelapse do not materially increase the
printer's mains load. The bed and hotend heaters dominate. The immediate return
of the network when the printer stopped instead points to electrical noise or a
marginal powerline link.

For the next session:

1. Put each powerline adapter directly in a wall socket, not a power strip.
2. Put the printer on a different wall outlet if possible. Do not modify its PSU.
3. Boot the printer and confirm the link while idle.
4. Confirm the link while heating, before starting a print.
5. Run the short tower without timelapse. If the link fails, repeat once with the
   USB camera unplugged to separate camera/USB load from heater/PSU interference.
6. Prefer direct Ethernet or Wi-Fi for printer monitoring if powerline remains
   unstable.

## Manual-cancel cleanup

The earlier 200 mm cleaning park changed only `END_PRINT`. The touchscreen and
Mainsail cancel buttons invoke the separate factory `CANCEL_PRINT` macro, so a
manual cancel never inherited that behavior.

The stock cancel macro also contains `printer.toolhead.homed_axe`, but Klipper's
documented status field and the maintained Mainsail, Klippain, Ellis, and
moonraker-timelapse macros use `homed_axes`. This typo can stop the factory
cancel template before its park and shutdown commands are emitted.

The later whole-stack audit found that patching only this factory block leaves
pause, resume, cancel, and completion with different owners. That earlier patch
is now quarantined. The preliminary runtime instead uses pinned Mainsail
`client.cfg` for pause/resume/cancel and a small T300 hook:

- Mainsail retracts if hot, turns heaters and the part fan off, clears pause
  state, and calls Klipper's native cancel;
- the T300 hook schedules its park for after that cancel path;
- movement occurs only when X, Y, and Z are all homed;
- Z rises to the greater of current Z + 10 mm or 200 mm, capped at Z maximum;
- the head never moves downward for cleaning and motors remain enabled;
- if position is unknown or optional parking fails, cancellation and heater
  shutdown have already occurred.

This follows the community pattern of sharing a guarded park behavior between
normal completion and cancellation while retaining the T300-specific lifecycle.

## Next printer session

1. Review the complete runtime proposal. Do not install the older standalone
   cleaning-height patch. A later approved install requires a firmware restart
   but no commanded motion or heating.
2. Print the temperature/stringing tower and watch the new purge behavior
   at startup. Keep hands and tweezers away from a moving toolhead; cancel if a
   loose blob threatens the print.
3. If cancellation is needed, verify that it retracts, raises Z to at least
   200 mm, parks at the factory XY position, and turns heaters off.
4. Inspect the six tower sections and check the powerline result.
5. Run Orca's standard retraction test at the selected temperature.
6. Put the measured temperature and retraction into the white-PLA profile.
7. Reslice and audit Frieren, then copy that final build to USB.

The current revised Frieren project and G-code are retained as review artifacts,
not as the next file to print.
