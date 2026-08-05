# OrcaSlicer runtime profile

The mainline T300 candidate changes the slicer contract. Orca should describe
the model, temperatures, filament, and per-layer frame requests; the printer
owns homing, meshing, standby heat, final heat, purge, end parking, and cancel
parking.

## Use this printer profile

Import or select:

```text
orcaslicer/T300 AUDITED Runtime 0.4 - REVIEW ONLY.json
```

It is intentionally marked review-only because the mainline firmware has not
yet been commissioned on the physical printer. Do not use it on the current
stock Comgrow firmware unless the matching runtime macros are installed.

## Required Orca settings

The profile pins these Orca-supported settings:

```text
G-code flavor: Klipper
Machine start G-code:
START_PRINT BED_TEMP=[bed_temperature_initial_layer_single] EXTRUDER_TEMP=[nozzle_temperature_initial_layer]

Machine end G-code:
END_PRINT

Print sequence: by layer
Label objects: on
Exclude objects: on
Power-loss recovery: printer configuration
Before layer change G-code: TIMELAPSE_TAKE_FRAME
```

Object labels and exclude-object output make Orca emit `EXCLUDE_OBJECT_DEFINE`
records before `START_PRINT`. The admission scanner and runtime purge use those
records to prove the purge lane and print bounds are safe. If object metadata is
missing, the printer refuses the file instead of guessing.

`Power-loss recovery: printer configuration` keeps Orca from injecting recovery
commands. The firmware side disables the vendor recovery path because the old
implementation could restore unverified positions after a power loss.

## What stays in normal Orca presets

Filament and print-quality tuning remain normal Orca work:

- temperature tower;
- flow ratio;
- pressure advance;
- retraction;
- maximum volumetric speed;
- supports, orientation, brim, seam, and ironing choices.

Do not bake temporary first-layer flow experiments into the shared printer
profile. The earlier `125%` bottom-solid flow came from the official Comgrow
process profile and is not treated as a validated fix for this printer.

## Migrating existing 3MF files

Use:

```bash
python3 ./bin/update-orca-kamp-startup.py source.3mf migrated.3mf --timelapse-per-layer
```

The migrator changes only supported Orca project settings, preserves embedded
model data, and refuses unknown start/end snippets or by-object print order.
Optional flags can set a reviewed initial nozzle temperature, bottom flow ratio,
printer retraction inheritance, or more infill-travel retractions for a specific
calibrated project.
