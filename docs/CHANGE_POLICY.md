# T300 change policy

This project uses a community-evidence gate for every live vendor-firmware
change and a separate release gate for the mainline candidate.
Offline inspection, backups, analysis, slicing previews, and test harnesses do
not modify the printer and may be performed freely. A configuration, macro,
firmware, calibration procedure, or generated print-start sequence may be
installed or used only when all of these conditions are met:

1. The behavior is documented by the printer manufacturer, official Klipper or
   OrcaSlicer documentation, or a traceable community project.
2. Printer-specific changes have evidence for the stock Comgrow/Sovol T300 and
   vendor firmware 1.5.2. Advice for another Sovol model or generic Klipper is
   context, not approval.
3. Compatibility and ownership conflicts with the vendor macros, GerGo macro,
   Klipper 0.12.0, the stock serial-TFT bridge, and stock hardware have been
   checked.
4. The exact source and revision are recorded, and the owner receives a simple
   explanation of the benefit, risk, and rollback.
5. The owner explicitly approves the live change before upload or restart.

An apparent error in the factory configuration is not permission to replace a
vendor value. It must pass the same evidence gate. When evidence is missing or
conflicting, preserve the current factory behavior and mark the proposal as
unapproved.

## Current decisions

- The purchased GerGo T300 Z-tilt-via-knob package is approved and remains the
  only gantry-alignment macro.
- Stock calibration commands and saved values remain under the vendor and
  official Klipper workflows.
- `t300_core.cfg` is locally authored and is quarantined. Its live include was
  removed on 2026-08-03; the inert file remains only as rollback evidence. Its
  installer exits before contacting or changing the printer. Prepared G-code
  that requires it must not be printed.
- The pinned KAMP subset is approved for `SMART_PARK` and `LINE_PURGE` only.
  KAMP's `Adaptive_Meshing.cfg` remains disabled: the vendor T300 native
  adaptive mesh completed successfully, while a T300 community report found
  the KAMP mesh override unreliable on this printer.
- Factory extrusion limits, idle-timeout behavior, resonance settings, and
  power-loss behavior remain unchanged on the live printer unless T300-specific
  evidence and owner approval support a particular replacement. A review-only
  runtime proposal now addresses the demonstrated idle, cancellation, purge,
  and unsafe recovery failures; it is not approved or installed merely because
  it exists locally.

This policy also applies when a generic upstream default appears safer or more
conventional than the vendor setting.

## Mainline candidate policy

The owner has separately approved building the pinned mainline replacement as
an offline candidate. That approval permits local source, tests, recovery
tools, and a commissioning-locked USB image. It does not permit changing the
live printer.

The mainline candidate may intentionally differ from vendor 1.5.2 only when the
difference has a named purpose and is covered by upstream documentation,
traceable community behavior, or an original implementation with tests and a
written audit. Verified T300 hardware values remain the starting point. Safety
ceilings may stay equal or become more conservative; no convenience feature
may raise them.

Moving the candidate through USB boot, eMMC installation, MCU flashing,
movement, heating, calibration, or print validation requires its own explicit
owner action at the physical printer. Passing offline tests is not approval to
cross one of those gates. Current-master CI is reporting-only and can never
produce a deployable stage; the stable release remains pinned to the exact
commit in `stack.lock.json`.
