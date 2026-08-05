# T300 mainline safety and operator-UI audit

Date: 2026-08-05

Status: laptop-side implementation only. Nothing described here has been
deployed to the T300, written to eMMC, flashed to either MCU, or used to command
movement or heat.

## Audit basis

The review used the exact pinned sources staged by this repository and the
following upstream documentation:

- Klipper command templates:
  <https://www.klipper3d.org/Command_Templates.html>
- Klipper API behavior:
  <https://www.klipper3d.org/API_Server.html>
- Klipper configuration reference:
  <https://www.klipper3d.org/Config_Reference.html>
- KlipperScreen configuration and custom menus:
  <https://klipperscreen.readthedocs.io/en/latest/Configuration/>
- KlipperScreen prompts and filament macro names:
  <https://klipperscreen.readthedocs.io/en/latest/macros/>
- Mainsail dashboard organization:
  <https://docs.mainsail.xyz/features/dashboard-organisation/>
- Official T300 limits and firmware page:
  <https://wiki.sovol3d.com/en/T300>

The live vendor printer was consulted read-only for version and configuration
comparison. No live file, service, setting, heater, motor, home, probe, or MCU
state was changed.

## Findings and dispositions

### Release blocker: Mainsail was staged as development source

An earlier candidate pointed nginx at the cloned Mainsail source tree. Its
`index.html` referenced TypeScript source and was not a production web build.
That could leave the UI blank and, more importantly, meant the deployed
artifact was not the release artifact that had been reviewed.

Disposition: the lock now names the official compiled `v2.18.2` ZIP, exact
size, and SHA-256. Extraction rejects traversal, links, encryption, duplicate
members, special files, excessive counts, and excessive expanded size. Staging
requires a matching `.version`, compiled JavaScript and CSS assets, and no
source references. Nginx serves `/opt/t300/www/mainsail`, which is read-only to
the service.

### High: a paused print could have bypassed one-use homing state

Upstream `virtual_sdcard.is_active()` becomes false while paused, while its
current file remains loaded. A design that checked only `is_active()` could let
an operator rearm and then home during a paused print.

Disposition: readiness cannot be set while any virtual-SD file is loaded.
`START_PRINT` consumes readiness before heat or movement and reserves one full
home for the exact active `ApprovedGCodeFile` object. The reservation requires
the command to originate from that file and is consumed before upstream `G28`.
Any loaded or paused file without that exact reservation is denied homing.

### High: a timeout/per-home prompt did not match the real workflow

A short-lived, one-use confirmation would repeatedly interrupt harmless
homing while still failing to describe why the surface needs another clean.

Disposition: the user-approved state is **cleaned and rearmed**, not a timer.
It is volatile and starts dirty after every Klipper start. It remains ready
through harmless actions and repeated idle homing. Filament-processing events
make it dirty again.

| Event | Resulting state |
| --- | --- |
| Klipper start or shutdown | Check required |
| **Cleaned and rearmed** confirmation | Ready |
| Idle X/Y home or repeated full home | Ready |
| Bed-only heating, file browsing, camera use | Unchanged |
| Admitted print startup reservation | Check required |
| Hotend target at extrusion temperature | Check required |
| Any G0/G1/G2/G3 command containing E | Check required |
| KAMP purge, load, unload, or M600 | Check required |
| Cancel, loaded-print command error, failed/incomplete print | Check required |

The extra marks conservative cases dirty, including an E parameter that results
in no physical extrusion. A false request to clean is safer than retaining a
false ready state.

### High: broad default touchscreen controls created misclick paths

KlipperScreen's defaults expose movement, heaters, extrusion, mesh, Z offset,
limits, pins, console, updates, and host controls. Those are useful during
maintenance but inappropriate on a small production screen.

Disposition: `use_default_menu: False` and an exact menu allowlist. The idle
screen exposes only the print workflow, human-checked homing, camera, and
notifications. The print menu exposes only camera and notifications; the
standard Job Status page owns Pause, Resume, Cancel, and Emergency Stop. The
screen action says **Clean & Rearm Plate**, while the prompt's affirmative
button says **Cleaned and rearmed**, so a static icon is not mistaken for a
live readiness indicator.

### High: ordinary cancellation is not immediate

Klipper documents that `gcode/script` and `pause_resume/cancel` can queue behind
an active command, including a temperature wait. This matches the delayed
cancel behavior previously observed on the vendor firmware.

Disposition: ordinary Cancel remains confirmed because accidental cancellation
is destructive. Emergency Stop remains unconfirmed and available through the
dedicated Klipper endpoint. Documentation and commissioning must teach that
Emergency Stop, not repeated Cancel clicks, is the immediate response during an
unsafe home, probe, or temperature wait. Error cleanup turns heaters off before
optional parking.

### Medium: KlipperScreen filament buttons require exact macro names

KlipperScreen calls `LOAD_FILAMENT` and `UNLOAD_FILAMENT`; private T300-prefixed
names would not be used by its built-in integration.

Disposition: the production macros now use the exact upstream names. Both
require an already paused print, Klipper's hot-extrusion state, and a bounded
length. Both invalidate build-plate readiness before moving filament.

### Medium: Mainsail defaults could encourage unsafe shortcuts

Upload-and-Print combines admission, selection, and start into one asynchronous
UI action. Generic dashboards also place motion and heater sliders near normal
monitoring controls.

Disposition: Upload-and-Print is hidden. Mobile, tablet, desktop, and widescreen
defaults show only Webcam and **Owner Actions**. Emergency Stop is immediate;
ordinary Cancel asks for confirmation; touch sliders are locked; Machine,
Console, Heightmap, and raw control panels are hidden. The same exact defaults
are installed as a root-owned Mainsail theme seed.

Mainsail settings are convenience controls, not access control. A user can
customize their browser layout later. The Klipper extra and G-code admission
policy remain authoritative if a panel is revealed.

## Preserved hardware safeguards

- Upstream Klipper retains heater control, sensor range checks, heater
  verification, endstop/probe handling, MCU shutdown, and watchdog behavior.
- `M112` is not renamed or wrapped.
- Production rejects forced movement, fake positions, raw pins/heaters, raw
  TMC fields, shell commands, firmware/config writes, and calibration writes.
- Policy ceilings match the verified stock motion/current configuration and
  the manufacturer's `300 C` nozzle and `100 C` bed ratings.
- Runtime commands can reduce speed, acceleration, flow, and current but cannot
  raise reviewed ceilings.
- Klipper has an exact by-ID MCU device allowlist. Other services have separate
  users, bounded devices, filesystems, memory, CPU/IO priority, tasks, and logs.
- Uploaded and USB G-code is scanned, snapshotted, hashed, and opened read-only.
  Unknown extended commands fail closed.
- Missing policy, approval, protected loader, compiled UI, service gate, or
  immutable configuration keeps the candidate unready.

## Residual risks

1. The T300 has no independent sensor for the removable build plate. Software
   cannot detect someone removing the steel sheet after confirming it. A real
   plate-presence sensor would be the only hardware-grade solution.
2. Ordinary Cancel may be delayed by a blocking Klipper command. Emergency Stop
   is the intentionally immediate option and causes a shutdown requiring
   inspection and restart.
3. UI hiding reduces mistakes but cannot establish physical truth. Direct API
   access from an explicitly trusted client can still invoke allowed macros.
4. Full-mesh compensation cannot repair a loose, sharply warped, damaged, or
   thermally unstable bed.
5. These changes do not tune Frieren stringing. Temperature, dry filament,
   pressure advance, retraction, flow, and volumetric-flow calibration remain
   post-migration work.
6. The candidate has not passed physical commissioning, a rollback drill, or a
   print. It must remain commissioning-locked until the owner completes those
   gates in person.

## Required validation before release

- Parse and test the exact Klipper `v0.13.0` configuration and retain vendor
  `v0.12.0` compatibility coverage.
- Test dirty/ready transitions, repeated idle home, paused print, wrong file,
  failed home, cancellation, error, restart, hotend heat, extrusion, purge,
  load, and unload.
- Verify every viewport's dashboard allowlist and the exact custom screen menu.
- Boot from recovery USB with printer control disabled and test touch targets,
  prompts, camera, storage, network, and restart behavior.
- Commission sensors, probe/endstops, fans, one low-speed axis at a time,
  cleaned-and-rearmed homing, one heater at a time, communication-loss shutdown,
  and Emergency Stop under attendance.
- Complete all calibration, a small print, a verified timelapse, soak testing,
  and a full vendor rollback drill before calling the stack production-ready.
