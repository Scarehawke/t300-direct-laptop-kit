# Stock T300 extruder rotation test

This procedure measures whether `50 mm` requested by Klipper produces `50 mm`
of filament movement. It is separate from OrcaSlicer's per-filament flow ratio.
The stock T300 currently uses `rotation_distance: 3.55` and already permits a
50 mm extruder-only move, so the test requires no configuration change.

## Physical setup

Use the currently loaded white PLA and digital calipers. Leave the build sheet
installed even though this test does not home. Place something below the nozzle
to catch the extruded filament. Measure at a fixed filament-entry point where
the filament can be kept straight without changing spool slack between readings.

## First measurement

1. From KlipperScreen or Mainsail, set the nozzle to `215 C` and wait for it.
2. In the console, run:

   ```gcode
   M83
   G1 E5 F60
   ```

   This slowly primes the nozzle and engages the extruder. Stop if the drive
   clicks, slips, or filament exits sideways.
3. Put a fine mark on the filament roughly `70 mm` before the chosen fixed
   entry point. Measure the exact distance with calipers and record it as
   `initial` rather than assuming the mark is exactly 70 mm.
4. Run:

   ```gcode
   M83
   G1 E50 F60
   ```

   The move takes 50 seconds. Do not use a touchscreen extrusion shortcut,
   because its speed may differ.
5. Measure the new distance from the same fixed point to the mark and record it
   as `final`.
6. Actual movement is `initial - final`.

## Repeat before changing anything

Make a new mark and repeat the 50 mm measurement at least once. Do not change
`rotation_distance` if the repeated actual distances disagree materially, the
extruder clicks, or filament flow from the nozzle is visibly inconsistent.
Those outcomes point to measurement slack, drive-grip trouble, or a hotend
restriction rather than a stable calibration error.

Klipper recommends repeating when the first result differs from the requested
distance by more than about 2 mm. For this first diagnosis, retain both results
even when they are close.

## Calculation

For each valid measurement:

```text
actual = initial - final
new_rotation_distance = 3.55 * actual / 50
```

The local calculator performs the same official Klipper calculation:

```bash
python3 ./bin/calc-extruder-rotation.py --initial INITIAL --final FINAL
```

Do not save the calculated value until two measurements agree. A candidate can
then be tested temporarily with:

```gcode
SET_EXTRUDER_ROTATION_DISTANCE EXTRUDER=extruder DISTANCE=VALUE
```

Repeat the measurement with the temporary value. Permanent editing and a
Klipper restart should happen only after the verification result is close to
50 mm and repeatable.
