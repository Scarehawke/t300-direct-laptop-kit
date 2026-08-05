# OrcaSlicer runtime profile

The mainline T300 candidate changes the slicer contract. Orca should describe
the model, temperatures, filament, and per-layer frame requests; the printer
owns homing, meshing, standby heat, final heat, purge, end parking, and cancel
parking.

## Install the reviewed preset set

The default machine is:

```text
orcaslicer/T300 AUDITED Runtime 0.4 - REVIEW ONLY.json
```

It is intentionally marked review-only because the mainline firmware has not
yet been commissioned on the physical printer. Do not use it on the current
stock Comgrow firmware unless the matching runtime macros are installed.

To install the machine preset as Orca's default for new projects:

```bash
python3 ./bin/install-orca-runtime-profile.py
python3 ./bin/install-orca-runtime-profile.py --apply
```

The first command is a dry run. The apply command installs two machine presets,
two calibration-required filament presets, and four review-only process
presets. It sets Orca's default machine to `T300 AUDITED Runtime 0.4 - REVIEW
ONLY` after backing up `OrcaSlicer.conf` and any matching user presets.

The installer detects the normal Orca config root on Linux, Windows, and macOS.
Use `--config-root` if a machine stores Orca profiles somewhere unusual. The
Windows default is `%APPDATA%\OrcaSlicer`.

Without Python, copy the JSON files from these repository folders:

```text
orcaslicer/T300 AUDITED Runtime 0.4 - REVIEW ONLY.json
orcaslicer/machine/
orcaslicer/filament/
orcaslicer/process/
```

to Orca's matching `machine`, `filament`, and `process` user-preset folders:

```text
Linux:   ~/.config/OrcaSlicer/user/default/machine/
Windows: %APPDATA%\OrcaSlicer\user\default\machine\
macOS:   ~/Library/Application Support/OrcaSlicer/user/default/machine/
```

Then select `T300 AUDITED Runtime 0.4 - REVIEW ONLY` as the printer in Orca.

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
Z-hop: 0 mm
Retract restart extra: 0 mm
Adaptive pressure advance: off
```

Object labels and exclude-object output make Orca emit `EXCLUDE_OBJECT_DEFINE`
records before `START_PRINT`. The admission scanner and runtime purge use those
records to prove the purge lane and print bounds are safe. If object metadata is
missing, the printer refuses the file instead of guessing.

`Power-loss recovery: printer configuration` keeps Orca from injecting recovery
commands. The firmware side disables the vendor recovery path because the old
implementation could restore unverified positions after a power loss.

The default uses zero Z-hop. Select `T300 AUDITED Runtime 0.4 - Collision
Clearance - REVIEW ONLY` only when Preview shows geometry that needs a 0.4 mm
travel lift.

## What stays in normal Orca presets

Filament and print-quality tuning remain normal Orca work:

- temperature tower;
- maximum volumetric speed;
- pressure advance;
- flow ratio;
- retraction;
- supports, orientation, brim, seam, and ironing choices.

Calibrate each filament in exactly that order: temperature, maximum volumetric
speed, pressure advance, flow ratio, then retraction. Pressure advance is saved
in the filament preset. The firmware accepts Orca's
`SET_PRESSURE_ADVANCE ADVANCE=...` only during an admitted print, from `0`
through `0.20`; it rejects smooth-time changes, alternate extruders, unknown
parameters, and uploaded `TUNING_TOWER`.

Orca requires numeric values in an editable project, so the calibration-required
profiles contain conservative placeholders: vendor-range temperatures, `12
mm3/s` maximum volumetric speed, `1.0` flow, `0` pressure advance, and `0.5 mm`
retraction. Their names are release gates, not claims that those values are
measured. Replace them only with accepted calibration results before final
export.

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

## Editable review projects

The current laptop-side review set is in:

```text
~/Downloads/T300-Orca-Review-20260805/
```

It contains the standard first-layer test, orange camera bracket, Frieren
quality candidate, and Frieren easier-support-release candidate. These are
editable `.3mf` projects, not final G-code. Artifact hashes and release gates
are recorded in `orcaslicer/staged-projects.json`.

Exact Orca 2.4.2 normal slices of the bracket and both Frieren candidates have
already passed the admission scanner. Those generated files were validation
evidence only. Final exports wait for physical commissioning, the relevant
filament calibration, Prepare and Preview review, and the acceptance order in
the Dossier.

## Hull- and floor-line review

Before final export, inspect Preview for a broad solid floor or deck that ends
while a thinner outer wall continues. That geometry can create a visible band
as the connected solid mass cools and shrinks. Layer-time changes may reveal
the same height but are not a reliable diagnosis or automatic cure.

The camera bracket has a concrete transition of this kind at `Z=1.20 mm`; the
organic Frieren exterior has lower confidence for one straight band. Warn the
owner when this pattern is likely, but do not automatically change overlap,
wall order, cooling, speed, supports, orientation, or model geometry. Those
remedies have material, strength, dimensional, surface, seam, or overhang
tradeoffs. The research and review procedure are in
`docs/ORCA_HULL_LINE_REVIEW.md`.
