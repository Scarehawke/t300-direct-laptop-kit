# T300 full software audit

Audit date: 2026-08-03

## Executive verdict

The current stack can complete an ordinary print, but it is not yet a robust
or unattended-printing setup. Its normal path works largely because Orca's
latest custom profile bypasses parts of the factory start macro. Its failure
paths are substantially weaker:

- the factory idle timeout no longer turns heaters off;
- ordinary pause, resume, runout, and cancel calls can fail during macro
  expansion;
- the power-loss path declares unmeasured axis positions as known;
- Orca's bundled T300 profile contains a stationary 25 mm prime and low-Z
  corner travel;
- several generations of mutually incompatible G-code and profiles remain on
  the laptop;
- the printer trusts every device on common private LAN ranges;
- backups do not capture the vendor code and scripts needed to reproduce the
  complete appliance.

The right response is not to tune every unusual factory number. First make one
component own the entire print lifecycle, restore unconditional heater
shutdown, quarantine unsafe recovery paths, and establish one versioned Orca
profile. Keep the vendor hardware limits unchanged until each one has a
measurement and T300-specific evidence behind a proposed change.

Do not run the current Frieren review file as a valuable five-hour print. It
passes the local static G-code bounds check, but it still uses the unvalidated
live lifecycle, 220 C, and an uncalibrated 0.8 mm retraction setup that already
produced severe stringing.

## Scope and confidence

### Audited directly

- the complete Moonraker `config`-root snapshot captured at
  `2026-08-03T21:01:52+02:00`;
- effective Klipper include order and duplicate-section ownership;
- factory motion, probe, mesh, heater, filament, and macro configuration;
- the installed, privately licensed GerGo macro's interface and offline test
  result, without reproducing its source;
- the pinned KAMP Smart Park and Line Purge subset;
- Moonraker, crowsnest, and moonraker-timelapse configuration;
- OrcaSlicer 2.4.2 system profiles, custom profile sources, 3MF projects, and
  generated G-code kept on this laptop;
- the laptop's network, deployment, backup, recording, static-audit, and test
  helpers;
- 56 repository unit tests and four exact upstream Klipper 0.12.0 harness
  cases;
- the latest Frieren G-code with the local strict preflight auditor.

### Still unavailable

This is a full audit of the software artifacts currently accessible from the
laptop, not a certification of the complete printer image. The printer was
powered off and was deliberately not contacted. The following still need a
read-only capture:

- the exact live configuration after the 21:01 backup and later uploads;
- the vendor-patched `bed_mesh.py` that implements the non-upstream
  `ADAPTIVE=1` option;
- `/home/mks/plr.sh`, `/home/mks/clear_plr.sh`, and the implementation of the
  non-upstream shell-command extension;
- KlipperScreen's vendor UI code and button-to-macro mapping;
- systemd service definitions, executable hashes, OS packages, web UI
  versions, MCU firmware, and bootloader;
- Moonraker's database, uploaded G-code, timelapse storage, free-space state,
  and retained logs.

The last reported software is vendor firmware 1.5.2, Klipper
`v0.12.0-113-g28f06a10-dirty`, and Moonraker
`v0.7.1-609-gbdd0222-dirty`. Generic component updates remain out of scope:
this is a vendor appliance, so replacing one embedded component can break the
screen, vendor extensions, or recovery code.

### Installed versus prepared state

| Component | Best established state | Confidence |
| --- | --- | --- |
| GerGo Z-tilt-via-knob | Included after the factory macros and installed | Confirmed by backup and completed calibration |
| KAMP Smart Park + Line Purge | Installed with `tip_distance: 3.5` and `purge_margin: 10` | Confirmed in 21:01 backup |
| KAMP adaptive meshing | Not installed; vendor adaptive meshing remains the owner | Confirmed in KAMP file and include closure |
| Filament sensor `.enabled` spelling repair | Present in factory `Macro.cfg` | Confirmed in 21:01 backup |
| END_PRINT minimum 200 mm clean height | `proposed-Macro.cfg` was produced by the apply transaction after the backup | Likely live at shutdown, but not read back afterward |
| CANCEL_PRINT clean-height patch | Present only in the newer laptop installer source | Not installed |
| KAMP `tip_distance: 0`, larger-margin proposal | Prepared only in laptop tooling | Not installed |
| `t300_core.cfg` | File exists in the config root but has no active include | Quarantined and inactive |
| Revised Frieren G-code | Generated on the laptop and statically audited | Review-only; not approved for printing |

The next session must compare live bytes rather than infer state from filenames.
No printer configuration was changed during this audit.

## Severity guide

- **P0 - safety or loss of control:** fix or quarantine before unattended or
  valuable prints.
- **P1 - high reliability risk:** fix before calling the setup repeatable.
- **P2 - quality, security, or maintainability debt:** measure and improve in a
  controlled phase.
- **Hold - suspicious but unproven:** preserve the vendor value until evidence
  supports a change.

## P0 findings

### P0-1: idle timeout can leave heaters on indefinitely

Evidence: `printer.cfg:32-38` replaces Klipper's default idle G-code with only
a `RESPOND` message. Defining this section replaces the documented default of
`TURN_OFF_HEATERS` followed by `M84`.

Impact: a forgotten manual preheat can remain hot after ten minutes. Heater
verification does not solve this; it detects abnormal heating response, not a
valid target that was simply forgotten.

Robust direction: restore `TURN_OFF_HEATERS` unconditionally. Motor shutdown
does not have to share the same policy. If holding Z alignment is important,
leave motors energized at the heater timeout and make motor release a separate,
explicitly tested policy.

Reference: [Klipper configuration reference](https://www.klipper3d.org/Config_Reference.html#idle_timeout).

### P0-2: factory cancel can fail before it cancels or cools

Evidence: `Macro.cfg:343` reads `printer.toolhead.homed_axe`; Klipper exposes
`homed_axes`. Klipper evaluates the whole Jinja template before executing its
generated commands. Therefore an expansion error can prevent the earlier
looking `CANCEL_PRINT_BASE` and the later `TURN_OFF_HEATERS` from executing at
all.

There is a second defect behind the typo: the non-paused branch attempts motion
when the homing test says the axes are *not* homed. A typo-only patch is not a
complete repair.

Impact: the local or web cancel button can appear delayed, return an error, and
leave the printer in an uncertain print/heater state. This matches the failure
shape observed during the aborted print.

Robust direction: replace cancel as part of the whole lifecycle package. Its
template must contain safe defaults only, call the native cancel path, turn off
heaters and fans regardless of homing state, and perform optional Z/XY parking
only for axes Klipper reports homed. A requested 200 mm cleaning height must
never lower a higher toolhead, exceed Z maximum, or move an unhomed axis.

References: [Klipper command templates](https://www.klipper3d.org/Command_Templates.html) and
[status fields](https://www.klipper3d.org/Status_Reference.html#toolhead).

### P0-3: ordinary pause and resume are malformed

Evidence:

- `PAUSE` at `Macro.cfg:210` accesses `params.STATE` even when a normal
  Mainsail or touchscreen call supplies no `STATE`;
- `RESUME` at `Macro.cfg:265` does the same;
- PAUSE retracts only while the nozzle is *below* its target, the opposite of
  the intended hot-enough condition;
- RESUME's normal path extrudes without checking `extruder.can_extrude`;
- the park condition uses `X differs AND Y differs`, so matching one coordinate
  can leave the other coordinate wrong;
- neither motion path has a complete homed-axis guard.

Impact: the most important recovery controls are least reliable precisely when
the print is already in trouble.

Robust direction: adopt one pinned, maintained pause/resume/cancel behavior as
a unit, using Mainsail's maintained `client.cfg` as the community reference.
Add only a small, reviewed T300 compatibility layer for touchscreen names and
the desired park position. Use defaults for every optional parameter,
`SAVE_GCODE_STATE`/`RESTORE_GCODE_STATE`, `homed_axes`, `can_extrude`, and
clipped axis limits.

Reference: [Mainsail community macros](https://github.com/mainsail-crew/mainsail-config/blob/master/client.cfg).

### P0-4: filament runout invokes pause twice

Evidence: the sensor has `pause_on_runout: True` and then calls `M600`.
Klipper's documented order pauses first and runs `runout_gcode` second. The
factory `M600` calls `PAUSE` again, without the required `STATE` parameter.

Impact: a real runout can enter the already broken pause macro twice, lose the
saved resume position, or fault instead of giving a clean filament-change
workflow.

Robust direction: choose exactly one owner. Either let `pause_on_runout` pause
and make the post-action non-pausing, or disable automatic pause and let one
tested M600 macro own it. Test runout, reload, resume, and cancel separately.

Reference: [Klipper filament switch sensor](https://www.klipper3d.org/Config_Reference.html#filament_switch_sensor).

### P0-5: power-loss resume invents XYZ coordinates

Evidence: `plr.cfg:49-59` enables `force_move`, sets X, Y, and Z to zero using
`SET_KINEMATIC_POSITION`, calls an uncaptured shell script, and prints a
generated recovery file. The shell script and touchscreen contract are not in
the backup.

Impact: Klipper is told that unmeasured axes are known. A wrong reconstructed
position can cause a collision, and no offline audit can establish what the
missing shell script injects.

Robust direction: quarantine `RESUME_INTERRUPTED` from normal UI use until the
complete extension, scripts, generated G-code, and screen flow are captured.
Test it only with a disposable model and an attended, documented power-cut
procedure. If it cannot re-establish trustworthy coordinates, remove the
feature instead of keeping a misleading button.

Reference: [Klipper force-move warning](https://www.klipper3d.org/Config_Reference.html#force_move).

### P0-6: the bundled Orca T300 profile contains a hazardous legacy start

Evidence: OrcaSlicer 2.4.2's built-in
`Comgrow T300 0.4 nozzle.json:86` does all of the following:

- moves to the X/Y corner at Z 0.3 mm;
- moves farther to negative X/Y coordinates;
- invokes the factory hidden-state `START_PRINT`;
- repeats the low corner positioning;
- heats fully and extrudes 25 mm while stationary;
- then begins a long corner wipe.

Impact: this exactly explains the earlier stationary corner line/blob behavior
when the stock profile or old G-code is selected. Because the custom profile
inherits the stock machine, selecting the wrong preset silently restores this
sequence.

Robust direction: never use the bundled T300 machine preset directly. Install
one clearly named user preset, such as `T300 AUDITED 0.4`, whose machine start
contains one parameterized macro call only. New projects must default to it,
and a laptop preflight should reject the legacy `G1 E25` signature.

### P0-7: software cannot detect the missing removable sheet

The bed incident exposed a hardware boundary, not merely a macro typo. The
stock inductive probe did not detect the underlying bed without the removable
steel sheet. No safe-home macro can prove the sheet is present before moving
far enough to sense it.

Robust direction: keep a mandatory physical sheet/nozzle/path check before
every home or calibration. A screen confirmation can reduce mistakes but is
not an interlock. True protection requires an independent sheet-presence
sensor or another positively validated hardware mechanism. Never test this by
intentionally homing without the sheet.

## P1 findings

### P1-1: lifecycle ownership is split and order-dependent

The active include order is:

1. `plr.cfg`
2. `timelapse.cfg`
3. `Macro.cfg`
4. `kamp_t300.cfg`
5. the private GerGo macro
6. `MCU_ID.cfg`

Factory start/end/pause/resume/cancel, KAMP park/purge, timelapse, PLR, Orca,
and the touchscreen each own part of the same state machine. GerGo intentionally
overrides the factory `Z_TILT_CALIBRATION` through a later merged section. That
private workflow passed its dedicated Klipper 0.12 harness, but the behavior
still depends on include order. `fluidd.cfg` contains a separate lifecycle
implementation but is currently dead because it is not included.

Impact: a small include, profile, or command-order change can select a different
owner without an obvious error. Old and new G-code then behave differently on
the same printer.

Robust direction: create an explicit owner map and one final compatibility
include. Keep GerGo as the only gantry-alignment owner. Keep inactive examples
outside the live config root. Give each public command exactly one owner.

### P1-2: factory start is a hidden two-pass state machine

`START_PRINT` has no slicer parameters. It reads whatever heater targets happen
to exist, saves the current filename before preflight, fully heats the nozzle,
probes, sets an internal `state`, and schedules itself again. Calling cancel in
the filament check does not stop the already-rendered remainder of the macro.
A failed cancel can also leave `state=Start`, changing the next print until the
state is reset or Klipper restarts.

Robust direction: replace it with one synchronous, parameterized command:
`PRINT_START BED=<target> EXTRUDER=<target>`. Validate inputs, establish one
known motion/extrusion mode, heat the bed, home, run the selected vendor mesh at
the actual bed target, park, heat the nozzle, perform a moving purge, and return
in a documented state. Exact standby temperature and timing need a pinned
community sequence plus an attended T300 test, not an improvised delay.

### P1-3: KAMP's installed tip prime explains the startup blob

The installed KAMP subset is pinned and intentionally does not install KAMP
adaptive meshing. That boundary is sound. The installed Line Purge setting is
not: `tip_distance` is 3.5 mm. KAMP implements it as stationary extrusion after
the nozzle is hot and before XY purge movement. It assumes the filament tip was
left 3.5 mm back by a matching prior end retract.

That assumption is false after a cancel, manual filament load, failed start, or
different G-code. The result is the observed ball before the purge line. KAMP
also derives its location from object polygons, which need not describe every
skirt, brim, and generated support footprint. A 10 mm margin can therefore
feel uncomfortably close.

Robust direction: move to KAMP's current upstream default `tip_distance: 0`
and a larger reviewed margin, then test starts from four states: fresh boot,
normal end, cancel, and manual load. Keep extrusion moving during the purge.
Do not call it a nozzle wipe: without a physical brush it can purge and break a
string, but cannot reliably clean the nozzle.

Reference: [KAMP settings](https://github.com/kyleisah/Klipper-Adaptive-Meshing-Purging/blob/main/Configuration/KAMP_Settings.cfg).

### P1-4: extrusion typo safeguards are effectively disabled

`max_extrude_cross_section` is 500 mm2. For a 0.4 mm nozzle Klipper's default
is 0.64 mm2. Extruder-only velocity is 2000 mm/s, acceleration is
10000 mm/s2, and instantaneous corner velocity is 10 mm/s. These are limits,
not normal print speeds, but they remove useful protection from malformed
G-code and macro mistakes.

These guards cover different failure classes. `max_extrude_cross_section`
limits an XY move carrying too much extrusion; E-only distance, velocity, and
acceleration govern stationary pushes. Narrowing the cross-section therefore
does not by itself prevent a stationary startup ball.

KAMP's Line Purge explicitly requires a cross-section limit of at least 5 mm2,
so blindly restoring 0.64 would break the selected community purge.

Robust direction: if Line Purge remains, reduce 500 to KAMP's documented
minimum of 5 only after the purge passes the whole-stack harness and an
attended test. Establish lower E-only limits from the real load/unload and
retraction requirements. Do not change motor current, heater limits, or motion
limits as part of this repair.

### P1-5: mesh wrapper discards caller intent

`BED_MESH_CALIBRATE` always forces 65 C, clears the mesh, and calls only
`BED_MESH_CALIBRATE_BASE ADAPTIVE=1`. It discards requested bounds, probe count,
profile name, and most other parameters. If the previous bed target was a
non-zero value other than 65 C, it does not restore it.

The `ADAPTIVE` option is a vendor extension absent from upstream Klipper 0.12.
The exact vendor implementation has not yet been captured, so replacing it
with generic KAMP meshing would be guesswork.

Robust direction: retain the working vendor adaptive engine for now, but put a
thin, explicit wrapper around the actual material bed temperature and forward
only parameters confirmed by its source. Capture and test the vendor
`bed_mesh.py` before changing mesh ownership.

### P1-6: the saved bed shape is exceptionally large

The latest saved 9x9 mesh has minimum `-1.081250`, maximum `-0.116875`, range
`0.964375 mm`, and mean about `-0.335224 mm`. This is far larger than normal
first-layer thickness. Adaptive meshing can compensate locally; it cannot make
the physical plate flat.

The configured `fade_start: 0`, `fade_end: 10`, and explicit
`fade_target: 0` progressively remove that correction and can alter effective
Z scale on a strongly warped surface. Klipper recommends understanding fade
target and mesh average before forcing a target.

Robust direction: do not edit mesh points manually. Measure probe repeatability
and three repeated full meshes at the same stabilized temperature, then compare
the matrices. Measure axis twist before enabling compensation. Change fade or
hardware only after the data separates probe noise, gantry alignment, sheet
shape, and bed shape.

References: [Klipper bed mesh](https://www.klipper3d.org/Bed_Mesh.html) and
[probe calibration](https://www.klipper3d.org/Probe_Calibrate.html).

### P1-7: load and unload can leak relative positioning state

Factory load/unload macros switch to `G91`, extrude without checking
`can_extrude`, and rely on later lines to return to `G90`. If cold extrusion
throws an error, the restore line may never execute and the next motion may run
in the wrong coordinate mode.

Robust direction: use `SAVE_GCODE_STATE` and `RESTORE_GCODE_STATE`, refuse cold
extrusion before changing modes, parameterize sane distances, and test local
screen buttons as well as console calls.

### P1-8: artifact drift makes the selected software unknowable

At least three incompatible generations exist:

- the current compact custom profile uses vendor adaptive mesh plus KAMP;
- older resolved profiles call hidden-state `START_PRINT` and contain a
  handmade purge;
- older generated calibration G-code calls the factory macro directly;
- newer full-mesh tests bypass the wrapper;
- the revised Frieren file uses the newest direct startup but still has
  provisional retraction and temperature.

All audited custom profile sources and editable 3MF files live under ignored
`.cache`. No T300 presets are installed under Orca's user profile directory.
This is why an old USB file could silently reproduce behavior believed to be
removed.

Robust direction: version-control the public machine/process/filament profile
sources, keep licensed models and private 3MF projects in a clearly named
private project directory, install a versioned Orca bundle, and generate one
manifest per G-code containing profile hashes and the expected macro-package
version. The USB should contain only explicitly approved current outputs;
everything else belongs in a dated laptop archive.

### P1-9: Moonraker trusts the whole private-address Internet edge

Moonraker listens on `0.0.0.0:7125` and trusts all of `10/8`, `172.16/12`, and
`192.168/16`. On the current home/powerline LAN, any device in those ranges may
receive trusted-client control. HTTP is unencrypted, so this is suitable only
for a genuinely trusted isolated LAN.

The laptop helper calls its direct connection "private" but configures
NetworkManager `ipv4.method shared`. NetworkManager documents that this starts
DHCP/DNS, enables forwarding, and NATs clients to the laptop's default network.
It therefore gives the printer outbound network access rather than providing a
strictly isolated link.

Robust direction: prefer a dedicated VLAN or direct cable with local DHCP and
forwarding blocked. Narrow Moonraker trusted clients to the actual management
host/subnet and use an API key or user authentication. Never port-forward the
printer. Treat PLC or Wi-Fi loss as expected: local printer controls must remain
the safety path.

References: [Moonraker authorization](https://moonraker.readthedocs.io/en/latest/external_api/authorization/)
and [NetworkManager shared mode](https://www.networkmanager.dev/docs/api/latest/nm-settings-nmcli.html).

### P1-10: deployment is backed up, but not transactional

Good properties already exist: uploads are size-limited, paths are validated,
sources are pinned, dry-run is the default, configuration is backed up before
apply, and failed Klipper readiness triggers rollback.

Remaining defects:

- if upload or restart raises before the readiness check, rollback is skipped;
- an upload can leave a new orphan file when a later step fails;
- the live file hash and idle state are not rechecked immediately before each
  write, so a concurrent screen/user change can be overwritten;
- upload response is trusted without downloading and verifying the stored
  bytes;
- `SHA256SUMS` paths are relative to `config-root`, but the file is stored one
  directory above, so conventional `sha256sum -c SHA256SUMS` fails;
- the backup omits the external scripts, vendor Python extensions, Moonraker
  database, UI mappings, service definitions, and executable versions needed
  for full recovery.

Robust direction: stage all new files, recheck idle state and compare-and-swap
hashes, upload the include switch last, verify bytes, restart, and roll back
from every exception path. Record every changed file and delete newly created
files during rollback. Fix checksum paths and add a documented restore test.

## P2 findings

### P2-1: several factory calibration values are unverified ceilings

- mesh travel is 400 mm/s;
- resonance excitation is 200 mm/s2 per Hz versus upstream's documented 75
  baseline;
- input-shaper damping ratios are 0.01 on both axes;
- machine ceilings are 600 mm/s and 12000 mm/s2;
- the current Frieren review file itself reaches only 250 mm/s and 4000 mm/s2.

These values are not proof of a fault, and the vendor may have hardware-specific
reasons. Run `PROBE_ACCURACY`, repeated meshes, and fresh ADXL captures before
changing them. The slightly rocking table must be stabilized before resonance
testing is meaningful.

### P2-2: factory probe calibration includes an automatic 4 mm descent

The wrapper heats the bed to 65 C, homes, invokes native probe calibration, and
then runs `TESTZ z=-4`. It is an attended convenience with a large automatic
step and no sheet-presence protection.

Robust direction: keep calibration user-initiated from the local console, as
requested. Show the exact next action and let the owner advance each step. Do
not automatically chain GerGo alignment, probe offset, and mesh generation.

### P2-3: M109 and M190 no longer have normal command compatibility

Factory wrappers require `S`, rebuild the heater command, and wait inside a
one-degree band. Files using other valid forms can fail, and generic G-code no
longer receives native behavior.

Robust direction: inventory touchscreen, PLR, and old-file dependencies before
removing the wrappers. New lifecycle code should use native heater commands or
explicitly named helper macros rather than redefining standard G-code.

### P2-4: camera and timelapse are usable but operationally brittle

- crowsnest is fixed at 640x480 MJPEG and 15 FPS;
- verbose logs are deleted on every restart, removing diagnostic history;
- timelapse writes frames, a rendered video, and a frame ZIP to internal
  printer storage with no visible retention or free-space policy;
- autorender adds post-print CPU and storage load;
- the laptop recorder hardcodes a fallback IP, opens a second MJPEG stream,
  transcodes continuously, has no reconnect loop, no preflight free-space
  check, and no retention policy.

The layer timelapse itself uses one snapshot per layer and `parkhead: false`,
so it should not add printhead motion or consume full-frame-rate storage. A
second live recorder can, however, add PLC traffic. Neither explains support
geometry or extrusion stringing.

Robust direction: first query the camera's real formats with `v4l2-ctl`.
Preserve and rotate crowsnest logs, establish a storage budget, disable frame
ZIP retention unless it is wanted, and make laptop recording discover the
host, reconnect, segment files, and fail clearly on low disk space.

Reference: [moonraker-timelapse configuration](https://github.com/mainsail-crew/moonraker-timelapse/blob/main/docs/configuration.md).

### P2-5: the first-layer test is geometrically intended to fuse

The latest 100% flow test requests a nominal 0.500479 mm first-layer bead. Its
diagonal centerlines are about 0.4575 mm apart perpendicular to the lines, an
intended overlap of roughly 0.043 mm or 8.6 percent. The model is not designed
to leave every strand separate.

The 125% bottom-surface flow came from the official Comgrow process profile.
It affects the relevant bottom surface, not every gram of an entire model, but
it is still a poor production default and masks root causes in a calibration.
The recent tests also changed temperature, flow, and mesh strategy between
runs, so they were not a clean one-variable comparison.

Robust direction: return production flow to 100%, verify extruder rotation with
the official measured-extrusion method, then use Orca's built-in temperature,
flow-ratio, pressure-advance, and retraction calibrations one variable at a
time. Do not manually alter mesh geometry or hand-space toolpaths.

Reference: [OrcaSlicer calibration guide](https://github.com/OrcaSlicer/OrcaSlicer/wiki/Calibration).

### P2-6: static checks give more confidence than their scope supports

The repository's 56 unit tests pass. Four process-level tests also pass on
exact upstream Klipper 0.12.0. Those four tests exercise the quarantined local
`t300_core.cfg`, not the active factory + PLR + timelapse + KAMP + private GerGo
include stack. Upstream 0.12 also lacks the vendor adaptive-mesh extension.

The G-code auditor correctly checks direct temperatures, acceleration, speed,
flow, bounds, and timelapse count, but it cannot expand printer macros. It did
not see KAMP's stationary 3.5 mm tip push or its use of the printer's 600 mm/s
travel ceiling.

Robust direction: add an exact whole-stack compile harness using captured
vendor extras, plus state tests for lifecycle macros. Extend G-code preflight
with macro-package metadata and known-dangerous signatures. Keep the static
auditor, but label its guarantees precisely.

## Hold: do not change without measurements

The audit does not recommend restoring generic Klipper defaults across the
board. Preserve these vendor settings until their hardware role is understood:

- heater sensor types, PID values, heater verification windows, and maximum
  temperatures;
- TMC current, sensorless-homing thresholds, microsteps, rotation distances,
  and step timing;
- axis position limits and homing speeds;
- maximum XY/Z speed and acceleration;
- probe speed, sample count, and tolerance;
- input shaper values until the ADXL345 and table setup are verified;
- mesh fade until repeated meshes and dimensional tests quantify its effect;
- GerGo's private implementation and settings except through its documented
  owner workflow.

Unusual is not the same as wrong. These values become change candidates only
when official Klipper/vendor documentation, a pinned community implementation,
or repeatable T300 measurements support the exact proposal.

## Robust target architecture

### 1. Freeze and inventory the vendor appliance

Keep firmware 1.5.2 intact. Capture the missing source/extensions, scripts,
services, UI mapping, database metadata, versions, storage status, and hashes.
Produce a restore manifest before another live modification. Do not use the
generic Mainsail update manager.

### 2. Separate hardware from behavior

Leave vendor pin, driver, heater, probe, and kinematic definitions in place.
Move custom behavior into clearly versioned includes with this ownership:

| Area | Single owner |
| --- | --- |
| Gantry alignment | private GerGo package |
| Start/end/pause/resume/cancel/runout | one audited lifecycle package |
| Mesh computation | vendor adaptive extension until its source is audited |
| Pre-print park/purge | pinned KAMP subset |
| Timelapse frame capture | moonraker-timelapse, non-parking mode |
| Calibration | explicit owner-run native commands |
| Slicing | one installed, versioned Orca T300 user profile |

No inactive macro examples should remain in the live config root.

### 3. Make failure handling independent of motion

Heater/fan shutdown and native cancel must work even when axes are unhomed,
filament is absent, a print is paused, or a parameter is missing. Parking is a
best-effort second step guarded by homing and limits. Normal end and manual
cancel may raise to at least 200 mm only when Z is known, never by moving down.

### 4. Give the slicer one contract

Orca should emit one start call and one end call. The printer macro owns
heating, homing, meshing, parking, and purge. The generated file should carry a
commented profile hash and macro ABI. Laptop preflight rejects mismatches,
legacy stationary prime signatures, temperatures outside the selected spool,
and unapproved review filenames.

### 5. Keep mesh dynamic but evidence-driven

Continue using Orca-supported object labels and the working vendor adaptive
mesh. Do not hand-edit models or mesh points. Validate probe repeatability,
axis twist, stabilization time, and local/full mesh agreement before changing
point density or installing hardware spacers. A physically flatter bed is
preferable to asking software to fade nearly 1 mm of compensation.

### 6. Make deployment recoverable

Every apply should be a transaction: fresh backup, live hash check, idle-state
check, staged uploads, include switch last, byte verification, firmware restart,
whole-stack readiness check, and rollback for every exception. Keep licensed
GerGo data private and third-party GPL components separate from the MIT code.

### 7. Treat network and video as optional services

The printer must finish safely with the laptop, PLC, camera, and Internet all
absent. Use an isolated management network, local touchscreen for emergency
control, internal non-parking timelapse with storage limits, and a reconnecting
laptop recorder only as an extra observer.

## Migration plan

### Phase 0: stop the risky paths

1. Do not use the stock Orca T300 preset, current Frieren review G-code, PLR
   resume, or unattended printing.
2. Keep the printer powered off until an owner-attended maintenance session.
3. Archive old USB G-code away from the printer-facing folder.

### Phase 1: read-only appliance capture

1. Power on with the build sheet installed and bed clear.
2. Rediscover the printer and take a fresh config backup.
3. Capture versions, active include closure, vendor `bed_mesh.py`, shell-command
   extension, PLR scripts, UI mappings, services, storage, and logs.
4. Compare the live bytes with the 21:01 snapshot and every proposed file. Do
   not move or heat the printer in this phase.

### Phase 2: offline package and tests

1. Build one lifecycle package from pinned maintained community behavior plus
   the minimum T300 compatibility adapter.
2. Restore unconditional idle heater shutdown.
3. Resolve cancel, pause/resume, M600/runout, load/unload, start, and end as one
   state machine.
4. Set KAMP tip distance to its upstream default of zero and review purge
   margin against full generated footprints.
5. Add exact whole-stack Klipper tests and deployment failure injection.
6. Build and install one canonical Orca user profile; mark every older output
   review-only.

### Phase 3: attended lifecycle acceptance

Use a tiny disposable print. The owner initiates each test locally and retains
physical control:

1. normal start from fresh boot;
2. start after normal end, cancel, and manual filament load;
3. ordinary pause and resume with no custom parameters;
4. filament runout, reload, and resume exactly once;
5. cancel while printing, paused, cold, hot, and with only some axes homed;
6. forgotten preheat followed by idle heater shutdown;
7. normal end and cancel on short and tall models, verifying no downward park;
8. network/camera disconnect while a disposable print continues locally.

No test should intentionally omit the build sheet or defeat a hardware
safeguard.

### Phase 4: measured calibration

1. GerGo gantry alignment, owner initiated.
2. `PROBE_ACCURACY` cold and at the actual PLA bed temperature.
3. Axis-twist measurement; enable compensation only if repeatable.
4. Probe Z offset, owner controlled.
5. Three full meshes at one stabilized temperature and matrix comparison.
6. Extruder rotation-distance measurement, changing it only for a repeatable
   error.
7. Orca temperature tower for the actual spool.
8. Orca flow-ratio, pressure-advance, and retraction tests in a documented
   order, one variable at a time.
9. ADXL input-shaper measurement only after the printer support is stable.

### Phase 5: production artifacts

1. Slice the camera mount first with the frozen profile and orange PLA.
2. Confirm purge, first layer, stringing, supports, normal end, and timelapse.
3. Re-open the Frieren 3MF, regenerate automatic supports in Orca, inspect the
   preview, and emit a new production G-code with matching hashes.
4. Preflight the final file and place only that approved file and a checksum on
   the printer USB.

## Immediate next-session decision list

The first live changes should be narrowly limited to P0 lifecycle safety after
the read-only capture:

1. restore heater shutdown on idle;
2. replace cancel/pause/resume/runout together;
3. quarantine PLR resume;
4. install one canonical Orca profile;
5. update KAMP tip prime only after the lifecycle contract is fixed;
6. run the attended acceptance matrix before any long print.

Everything else, including extrusion limits, mesh fade, probe speed, input
shaper, motor settings, and hardware bed changes, remains a measured proposal
rather than an automatic edit.

## Primary references

- [Official T300 firmware page](https://wiki.sovol3d.com/en/T300)
- [Klipper 0.12.0 source](https://github.com/Klipper3d/klipper/tree/v0.12.0)
- [Klipper configuration reference](https://www.klipper3d.org/Config_Reference.html)
- [Klipper command templates](https://www.klipper3d.org/Command_Templates.html)
- [Klipper status reference](https://www.klipper3d.org/Status_Reference.html)
- [Klipper bed mesh](https://www.klipper3d.org/Bed_Mesh.html)
- [Klipper probe calibration](https://www.klipper3d.org/Probe_Calibrate.html)
- [KAMP](https://github.com/kyleisah/Klipper-Adaptive-Meshing-Purging)
- [Mainsail config](https://github.com/mainsail-crew/mainsail-config)
- [Moonraker authorization](https://moonraker.readthedocs.io/en/latest/external_api/authorization/)
- [moonraker-timelapse](https://github.com/mainsail-crew/moonraker-timelapse)
- [OrcaSlicer calibration](https://github.com/OrcaSlicer/OrcaSlicer/wiki/Calibration)
- [NetworkManager IPv4 shared mode](https://www.networkmanager.dev/docs/api/latest/nm-settings-nmcli.html)
