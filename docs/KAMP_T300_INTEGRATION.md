# T300 native mesh with KAMP park and purge

> **State note:** this documents the installed KAMP subset and the older
> slicer-side startup. The 2026-08-04 preliminary runtime proposal keeps the
> same KAMP subset but moves startup ownership into `t300_runtime.cfg`. Do not
> combine the direct sequence below with that runtime.

## Installed decision

The stock T300 remains responsible for adaptive bed meshing. The installed
KAMP subset adds only two commands:

- `SMART_PARK` raises the nozzle to 10 mm and waits near the actual print area
  instead of an arbitrary bed corner.
- `LINE_PURGE` draws one moving purge line beside the actual print area after
  the nozzle has reached printing temperature.

KAMP's `Adaptive_Meshing.cfg` is not installed. The T300's vendor-patched
`BED_MESH_CALIBRATE ADAPTIVE=1` completed a correct object-sized 4 x 4 mesh in
the observed test, while a T300-specific community report found KAMP's mesh
override unreliable on this printer. Using both mesh implementations would
also give two macros ownership of the same command.

The source is pinned to KAMP revision
`b0dad8ec9ee31cb644b94e39d4b8a8fb9d6c9ba0`. The installer verifies the exact
upstream settings, Line Purge, and Smart Park file hashes before assembling
`kamp_t300.cfg`. It refuses to proceed if the T300 firmware, native mesh
wrapper, GerGo include, object labeling, or required purge allowance differs
from the reviewed machine.

The assembled settings retain KAMP's upstream `tip_distance: 0`. A live T300
test showed that priming 3.5 mm at a fixed XY point formed a ball before the
moving purge began. The 30 mm moving purge has ample length to recover the
factory print-end retract without a separate stationary extrusion.

The purge margin is increased from upstream KAMP's 10 mm default to 20 mm.
Orca's exclude-object polygon does not reliably include every support base,
brim, or skirt, so a nominal 10 mm object margin left the purge only a few
millimetres from the actual Frieren first-layer boundary. Twenty millimetres is
also the default used by the maintained KAMP LiTE configuration and remains an
ordinary KAMP setting rather than a new motion sequence.

## What did not change

The KAMP installation itself does not alter calibration values, Z offset, bed
mesh settings, motion limits, heater settings, factory lifecycle macros,
extrusion limits, GerGo leveling, or power-loss behavior. No calibration is
required merely because Smart Park and Line Purge were added. The separate
runtime proposal does change lifecycle ownership and the single extrusion
cross-section safeguard; those changes have their own review and tests.

The factory `START_PRINT` macro remains present for touchscreen compatibility,
but prepared Orca projects do not call it. Their reviewed start sequence is:

```gcode
M117
M140 S[first-layer bed temperature]
M104 S150
M190 S[first-layer bed temperature]
G28
BED_MESH_CALIBRATE ADAPTIVE=1
SMART_PARK
M109 S[first-layer nozzle temperature]
LINE_PURGE
```

KAMP documents Smart Park as the step before final nozzle heating and Line
Purge as the final step after heating. The 150 C standby comes from the owner's
purchased T300-specific GerGo workflow; final heating now occurs at Smart Park
and is followed immediately by a moving purge.

## Current test

The USB root contains `RUN_NEXT_FL_KAMP_215C.gcode`. Its editable OrcaSlicer
project is under the private editable-project directory on the USB and in the
laptop Downloads folder. The file was sliced with OrcaSlicer 2.4.2 and passed:

- exact startup-order and command audit;
- T300 motion, temperature, acceleration, and flow limits;
- one-layer geometry and extrusion audit;
- designed line-overlap proof (`0.0425 mm` overlap);
- USB copy checksum verification.

Before running any file that homes, physically confirm the removable metal
sheet is installed. The stock inductive probe cannot detect the underlying
plastic bed safely when that metal sheet is absent.

## Rollback

The automatic pre-install backup is stored locally under
`.cache/live-backups/20260803-kamp-subset-install/`. To roll back, remove
`[include kamp_t300.cfg]` from `printer.cfg`, restart Klipper, and optionally
delete the now-inert `kamp_t300.cfg`. The removed macros hold no saved state or
calibration data.

Source references:

- [KAMP](https://github.com/kyleisah/Klipper-Adaptive-Meshing-Purging)
- [T300 KAMP failure report](https://www.reddit.com/r/klippers/comments/1jqjcf6/error_while_using_kamp/)
