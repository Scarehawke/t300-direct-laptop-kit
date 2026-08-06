# T300 mainline safety and operator-UI audit

Date: updated 2026-08-06

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
| Successful idle X/Y home or repeated full home | Ready |
| Any failed X, Y, or Z home | Check required; Klipper remains in error |
| Bed-only heating, file browsing, camera use | Unchanged |
| Admitted print startup reservation | Check required |
| Hotend target at extrusion temperature | Check required |
| Any G0/G1/G2/G3 command containing E | Check required |
| KAMP purge, load, unload, or M600 | Check required |
| Cancel, loaded-print command error, failed/incomplete print | Check required |

The extra marks conservative cases dirty, including an E parameter that results
in no physical extrusion. A false request to clean is safer than retaining a
false ready state.

### Resolved owner review: preserve the exact stock touchscreen

The initial audit proposed replacing the physical display with a simplified
KlipperScreen interface. Owner review correctly identified that this removed
useful daily controls and did not resemble the familiar T300 screen.

Final disposition: preserve the exact Comgrow 1.5.2 serial-TFT firmware,
artwork, navigation, file browser, movement, temperature, tuning, print, and
Emergency Stop controls. The vendor bridge is confined behind an unprivileged
loopback compatibility gateway. It has no raw GPIO, shell, updater,
configuration-write, unrestricted Moonraker, or service-control authority.

All 77 known physical controls have an exact contract. The Macro page contains
only read-only **Printer Status**; Pause, Resume, Stop, and Change Filament stay
in their dedicated stock locations. **Home Z** and **Home all** require the
plate to be confirmed in Mainsail first because the TFT cannot display the
checkbox. **Home XY** never moves Z and preserves readiness after success, but
any failed home invalidates readiness and leaves Klipper in error. Backend
limits and guards, not missing UI controls, remain the safety boundary.

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

### Medium: touchscreen filament buttons require exact translation

The stock bridge uses vendor command forms that do not directly match the
production lifecycle macros.

Disposition: the gateway translates them to the production `LOAD_FILAMENT` and
`UNLOAD_FILAMENT` commands. Both require an attended idle or paused state,
Klipper's hot-extrusion state, and bounded movement. Both invalidate
build-plate readiness before moving filament.

### High: the vendor bridge restarted Klipper during screen startup

Serial tracing found that the bridge emits a hidden `FIRMWARE_RESTART` while it
initializes. That was part of the tightly coupled stock appliance boot sequence
but would create an unexplained Klipper restart when the display and printer
services are independently supervised.

Disposition: only the first hidden startup request is acknowledged without
execution. Later explicit **Restart** and **Firmware restart** controls retain
their advertised behavior. A display crash or restart therefore cannot restart
Klipper behind the operator's back.

### High: automatic Klipper crash restart hid the failure boundary

An automatic service restart after a real Klippy process crash could create a
new process while the operator is still diagnosing a safety-significant fault.

Disposition: production `klipper.service` uses `Restart=no`. MCU watchdogs and
heater shutdown remain independent. Explicit Klipper restart controls still
work while Klippy is alive; a process crash remains down for inspection and an
owner-operated restart or power cycle.

### Medium: Mainsail defaults could encourage unsafe shortcuts

Upload-and-Print combines admission, selection, and start into one asynchronous
UI action. Generic dashboards also place motion and heater sliders near normal
monitoring controls.

Disposition: only Upload-and-Print is hidden, because starting before the
asynchronous admission result would create a scanner race. Mobile, tablet,
desktop, and widescreen defaults preserve Mainsail's normal toolhead,
temperature, extrusion, macro, machine, console, camera, file, heightmap, and
G-code views. Emergency Stop is immediate; ordinary Cancel asks for
confirmation; touch sliders are locked. The exact defaults are installed as a
root-owned Mainsail theme seed.

Mainsail settings are convenience controls, not access control. The Klipper
extra and G-code admission policy remain authoritative for commands issued by
visible panels.

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
- Verify every Mainsail viewport and all 77 entries in the exact stock
  touchscreen button contract.
- Boot from recovery USB with printer control disabled and test touch targets,
  prompts, camera, storage, network, and restart behavior.
- Commission sensors, probe/endstops, fans, one low-speed axis at a time,
  cleaned-and-rearmed homing, one heater at a time, communication-loss shutdown,
  and Emergency Stop under attendance.
- Complete all calibration, a small print, a verified timelapse, soak testing,
  and a full vendor rollback drill before calling the stack production-ready.
