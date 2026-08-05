# T300 mainline implementation audit

## Audit scope

This is the post-implementation audit for the requested mainline migration.
It separates implemented offline controls from physical work that the owner
must still initiate. Passing this audit does not approve a live migration.

## Live baseline consulted read-only

The printer was found at `192.168.178.54` and inspected without sending motion,
heat, home, restart, service, config-write, calibration, or flash commands.

- Klipper: `v0.12.0-113-g28f06a10-dirty`.
- Moonraker: `v0.7.1-609-gbdd0222-dirty`.
- X/Y/Z ranges: `-2..302`, `-6..302`, `-5..370` mm.
- Velocity/acceleration: `600 mm/s`, `12000 mm/s2`.
- Z velocity/acceleration: `12 mm/s`, `100 mm/s2`.
- Probe offsets: X `20`, Y `-24`; full mesh `9x9`.
- Driver currents: X `1.1`, Y `1.5`, Z `0.75`, extruder `1.0 A`.
- No pending `SAVE_CONFIG` block or live configuration warning was reported.

The exact installed private GerGo file was streamed through a structural
checker without storing or displaying its content. It contains exactly three
G-code macro sections and none of the production-forbidden debug, raw output,
firmware, shell, config-write, or emergency-stop commands. This is not a
substitute for the still-required private v0.13 functional harness.

## Requirement matrix

| Area | State | Audit result |
| --- | --- | --- |
| Exact stack lock | Implemented | Sources, commits, archives, hashes, licenses, Python wheels, Debian packages, firmware inputs, and compatibility patches are locked. |
| Stable vs next | Implemented | Stable is exact v0.13.0. Current master runs in a separate allowed-to-fail CI job, emits `deployable: false`, and creates no stage or artifact. |
| Recovery USB | Implemented offline | Recovery stage, marker, restricted SSH agent, three-boot ledger, board/root/eMMC checks, and machine identity are present. Physical boots remain undone. |
| Full eMMC capture | Implemented offline | Capture streams every byte, hashes in flight, performs a second device hash, compresses, and writes a manifest atomically. |
| Filesystem image validation | Implemented offline | Root-only read-only loop, geometry comparison, `fsck -n`, read-only mount/read check, and fail-closed cleanup are implemented. No real image has been checked yet. |
| Vendor restore | Implemented offline | Restore requires matching identity, size, geometry, recovery state, typed confirmation, and complete target re-read. No restore drill has occurred. |
| Candidate USB provisioning | Implemented offline | Only the running signed removable USB root can be provisioned. eMMC and arbitrary roots are refused. No printer-control service is enabled. |
| eMMC candidate installation | Owner gate | Use official local `armbian-install` only after recovery and host tests. It is intentionally not automated by this repository. |
| Firmware | Build-only | STM32F401 and Linux host-MCU artifacts are reproducibly built and hashed. There is deliberately no flashing API. |
| Production hardware config | Implemented | Live pins, directions, ranges, probe geometry, sensors, currents, fans, and factory motion/extrusion values were transcribed. Old calibration values were excluded. |
| Safety policy | Implemented | Root-owned read-only policy, hard configuration checks, runtime speed/current guards, forbidden production commands, and commissioning lock fail closed. |
| Steel-sheet protection | Implemented with hardware limitation | One-use time-limited confirmation is required for Z home. The stock probe still cannot sense sheet presence directly. |
| G-code admission | Implemented | Uploaded and USB G-code is scanned, bounded, snapshotted read-only, and tied to the exact policy hash. |
| Lifecycle | Implemented | Validated start, full mesh, bounded KAMP purge, hot-only retract, heater-first cancel, clearance-aware park, runout, and idle behavior. |
| GerGo | Private boundary implemented | Excluded from production and included only in attended local maintenance. Exact purchased archive is still needed for the private v0.13 run. |
| Service isolation | Implemented | Separate users, narrow devices, immutable config, CPU/memory/task/file/log limits, and printer-priority weighting are statically checked. |
| Network boundary | Implemented | Moonraker/Crowsnest loopback-only; Nginx allows only loopback and the selected direct-laptop CIDR. Restricted SSH is normally disabled. |
| Camera/timelapse | Implemented | Stable by-ID camera, one non-parking frame per layer, USB-only bounded storage, verified rendering, and source retention on failure. |
| Orca integration | Implemented as review-only profile | Supported lifecycle and per-layer fields only; no model mesh or final G-code edits. Fresh slices remain unapproved until commissioning. |
| Calibration and prints | Not crossed | All movement, heating, calibration, first-layer, dimensional, bracket, and Frieren work remains owner-attended. |
| Final release | Locked | Requires physical safety, UI, prints, timelapse, soak, filesystem-image verification, and rollback evidence tied to the exact config. |

## Safety review

### Hardware protection retained

Upstream Klipper owns MCU watchdogs, communication failure, endstops, probe
errors, thermistor validity, heater verification, movement bounds, extrusion
temperature, and heater limits. The local extra wraps requests; it does not
replace those mechanisms. `M112` is not wrapped.

Normal production cannot use forced movement, fake homing positions, raw pins,
raw heater objects, arbitrary TMC registers, shell commands, runtime calibration
writes, or config writes. Maintenance can expose upstream debug behavior only
while normal Klipper and Moonraker are stopped, over a local PTY, with a
one-use owner marker.

### Failure ordering

Cancel and virtual-SD error paths turn heaters and the part fan off before
optional parking. Parking occurs only when all axes are homed, lifts first,
never lowers the head, respects Z maximum, and skips XY when less than 2 mm of
new clearance is available. Camera, timelapse, removable storage, UI, and
network failures do not send motion or heater commands.

### Remaining unavoidable risks

- The sheet confirmation is procedural. Only added hardware could truly detect
  whether the steel sheet is installed.
- Maintenance mode contains bypass commands by design and therefore requires
  physical attendance and a clear bed.
- Software cannot prove the nozzle, heater cartridge, thermistor, belts,
  connectors, bed, or mains wiring are physically undamaged.
- A correct mesh cannot repair a sharply deformed or moving stock bed.
- eMMC and MCU operations remain the highest-risk steps and are not inferred
  from offline success.

## Issues found during the implementation audit

1. Moonraker and Crowsnest originally had a broader remote exposure in the
   candidate. They now bind loopback; a CIDR-limited Nginx gateway is the only
   remote entry point.
2. Private GerGo was initially visible to production. It is now absent from the
   production include tree and available only in maintenance.
3. Release acceptance originally lacked verified image-filesystem evidence.
   The new root-only read-only loop and `fsck -n` result is now mandatory.
4. Vendor Klipper 0.12 regression coverage was informative but not mandatory.
   It is now a required validation-report field.
5. Current-master compatibility was metadata only. It now runs the same 25
   production safety cases in a non-deploying CI lane.
6. Crowsnest and Xorg had global journal limits but no per-file limit. Both now
   have a 16 MiB `LimitFSIZE`, enforced by static validation.
7. The reusable Orca profile defined the timelapse macro but did not request
   layer frames. It now uses Orca's supported before-layer-change field without
   adding parking or motion.

## Validation evidence

At the time of this audit:

- The exact pinned Klipper v0.13 harness passes 25 production cases.
- A freshly fetched current Klipper master at
  `9c1ae230eaebd5ec4df76d5a87537e2f35defab0` passes the same 25 cases in
  reporting-only mode.
- The vendor Klipper 0.12 harness remains part of required release validation.
- Unit, stage, lifecycle, systemd, network, secret, policy, imaging, transfer,
  provisioning, firmware-build, timelapse, and configuration-deployment tests
  are part of the generated validation report.

The final test count and fresh candidate-stage manifest belong in generated
validation output, not this prose file, because either changes whenever a test
or staged byte changes.

## Deliberately unfinished physical work

The following cannot honestly be marked complete from the laptop:

1. Three distinct recovery USB boots and exact MKS-Klipad50/eMMC confirmation.
2. Full vendor eMMC capture, read-only filesystem check, and private backup
   storage.
3. Screen, touch, backlight, fan, Ethernet, Wi-Fi, USB, camera, and storage test
   on the candidate USB.
4. Official interactive copy from the validated USB candidate to eMMC.
5. Bootloader-method and original-recovery proof for both MCU targets.
6. MCU communication-loss and Emergency Stop test on hardware.
7. Every listed calibration and all test prints.
8. Timelapse rendering, network/camera soak, and full vendor rollback drill.

Until those gates pass, the correct release state is **commissioning locked**.
