# T300 mainline migration

## Read this first

The live T300 still runs Comgrow firmware 1.5.2. Nothing in the mainline
candidate has been installed on the printer, written to eMMC, or flashed to an
MCU. Keep using the vendor rollback path until every physical gate below has
been completed successfully.

The owner initiates every power change, boot selection, disk write, firmware
flash, movement, heater test, and calibration. The tools may inspect and verify
results, but they do not take those actions on their own.

## What the candidate changes

### A reproducible software base

The candidate uses one exact version of each component instead of an
interactive updater:

- Armbian 26.5.1 Debian 13 for MKS-Klipad50, kernel 6.18.33.
- Klipper v0.13.0 at commit `61c0c8d2ef40340781835dd53fb04cc7a454e37a`.
- Moonraker 0.10.0, Mainsail 2.18.2, KlipperScreen 0.4.7, and Crowsnest 5.0.0.
- Pinned Mainsail client macros, KAMP park/purge, and Moonraker timelapse.

Every archive, package, wheel, source commit, patch, hash, and license is named
by `stack.lock.json` and the two offline package locks. Mainsail has no update
manager. Updating one component means building and validating a new lock, not
changing the printer in place.

### Hardware limits remain authoritative

The stock T300 pins, motor directions, sensor types, travel, probe offsets,
driver currents, fans, and factory motion ceilings were copied only after a
read-only comparison with the live machine. The candidate starts with:

- X travel `-2..302`, Y travel `-6..302`, and Z travel `-5..370` mm.
- Maximum velocity `600 mm/s` and acceleration `12000 mm/s2`.
- X/Y/Z/extruder currents `1.1/1.5/0.75/1.0 A`.
- Hotend ceiling `300 C` and bed ceiling `100 C`.

The temperatures are deliberately below the live configuration's `305 C` and
`105 C` limits. Runtime commands can reduce speed, flow, acceleration, or
current, but cannot raise the reviewed ceilings.

### A fail-closed production policy

The small `t300_safety` Klipper extra does not drive heaters or motors. Upstream
Klipper retains that job. The extra checks the immutable policy when Klipper
connects and refuses to become ready if the policy, permissions, machine
limits, KAMP settings, or protected G-code loader do not match.

Production mode blocks commands that bypass normal safety and configuration,
including forced movement, fake positions, raw output pins, raw heater objects,
driver-register writes, shell commands, calibration writes, and `SAVE_CONFIG`.
`M112` remains Klipper's untouched emergency stop.

Maintenance mode is a separate local-only Klipper process. Normal Klipper and
Moonraker must be stopped first, maintenance is armed once by a root-owned
marker, and it has no network. There is no G-code command that switches modes.

### Cleaned-and-rearmed build-plate state

The stock inductive probe detects metal, but it cannot prove that the removable
steel sheet is installed. The owner therefore uses the human-facing **Clean &
Rearm Plate** action after fitting the sheet, latching both clips, cleaning the
surface, removing loose filament, and clearing the motion path. Its prompt has
one affirmative action: **Cleaned and rearmed**.

That volatile check remains valid across harmless actions and repeated idle
homing. It is invalidated by every admitted print before heating or motion,
every hotend target at or above extrusion temperature, every commanded
extrusion or retraction, purge, filament load/unload, cancellation, loaded-print
error, Klipper shutdown, or restart. The next print or idle Z home then opens or
requires the check again. Bed-only heating, camera use, file browsing, and other
actions that do not process filament do not invalidate it.

Print startup consumes the state and binds one full `G28` authorization to the
exact active, admitted, immutable G-code file. A paused or merely loaded file
cannot be rearmed and cannot home. A failed home cannot reuse the authorization.
This would have prevented the earlier missing-sheet accident, but it remains a
human confirmation rather than a real plate sensor. Manually removing the sheet
after confirming it is not detectable in software.

### Uploaded G-code is quarantined

Moonraker writes uploads to the removable data USB. A separate unprivileged
scanner accepts only bounded Orca-style print files with:

- object polygons before exactly one `START_PRINT`;
- exactly one `END_PRINT`;
- reviewed temperatures, motion, extrusion, percentage limits, and bounded
  admitted-print pressure advance;
- no homing, mesh, firmware, shell, config-write, debug, or legacy lifecycle
  commands in the file;
- a bounded KAMP purge lane and bounded object data.

Klipper opens a read-only protected snapshot, not the mutable uploaded file.
Changing the upload or policy invalidates its approval.

### A simpler start sequence

Orca emits only:

```text
START_PRINT BED_TEMP=... EXTRUDER_TEMP=...
```

The reusable Orca profile also pins Klipper G-code flavor, by-layer print order,
object labels, exclude-object output, and printer-owned power-loss recovery. The
object records are part of the safety interface: without them the scanner and
runtime refuse to choose a purge lane.

The immutable macro then checks object data and the build-plate state, sets a
`150 C` standby nozzle, heats the bed, homes through the normal probe path,
creates a fresh full `9x9` mesh at bed temperature, parks, finishes nozzle
heating, resets pressure advance, and uses KAMP's moving line purge. There is no
stationary priming and no handwritten slicer wipe. A stationary positive E move
is accepted only when it recovers filament that the same print already
retracted; the old generic 5 mm allowance is gone. Orca's five-decimal E
formatting is accounted for with exact decimal arithmetic. Each discrepancy is
bounded by the number of quantized retract/wipe terms, requires intervening
moving deposition before another exception, and contributes to a cumulative
allowance of at most one hundred-thousandth of actual moving extrusion plus one
0.0001 mm startup/path allowance. There is no fixed whole-file ceiling that
would reject a valid long print merely because it has more layers.

The admission harness includes a deterministic 900-layer, 250 x 234 x 270 mm
digital job representing about 1.5 kg of deposited PLA, 36,000 retract/wipe
recoveries, pressure advance, layer timelapse calls, protected snapshotting and
approval-record creation. It admits 0.360 mm of cumulative five-decimal
discrepancy while rejecting both repeated stationary recovery and attempts to
farm allowance with microscopic moving extrusions.

KAMP `tip_distance` is zero, its object margin is 20 mm, and the local patch
adds explicit axis bounds and a short breakaway move. A crowded plate with no
safe front or left purge lane is refused before heating.

### Predictable finish, cancel, runout, and idle behavior

`END_PRINT` retracts only while hot, turns heaters and the part fan off, lifts
before XY travel, and parks at or above Z 200 without lowering the head.
`CANCEL_PRINT` turns heaters off before its optional retract and park. Both skip
motion when axes are unhomed and skip XY travel when safe Z clearance is too
small.

Runout pauses once. Filament load and unload require an already paused printer
and a hot enough nozzle. Ten minutes of idle time turns heaters and the part fan
off but leaves motors enabled.

Ordinary Pause, Resume, and Cancel use Klipper's queued G-code path. A Cancel
request can therefore wait behind an active homing, probing, or temperature-wait
command. The always-visible Emergency Stop remains unconfirmed and uses
Klipper's immediate shutdown endpoint. It is the correct interruption when
waiting for ordinary Cancel would be unsafe.

### Deliberately small operator interfaces

KlipperScreen replaces its broad default menus with a short touch workflow:
**Print**, **Clean & Rearm Plate**, **Select Print File**, **Home Printer**,
**Camera**, and **Notifications**. Raw movement, extrusion, temperature, mesh,
Z-calibration, current/limit, pin, console, updater, and system controls are not
placed on the production screen. Job Status retains its familiar Pause, Resume,
Cancel, and Emergency Stop controls.

Mainsail starts with icons and text, a confirmed ordinary Cancel, an immediate
Emergency Stop, locked touch sliders, no Upload-and-Print shortcut, and one
small **Owner Actions** group. Raw toolhead, heater, extruder, machine, console,
and limit panels are hidden on mobile, tablet, desktop, and widescreen layouts.
The UI defaults reduce misclicks; they are not the safety boundary. Revealing a
panel later does not bypass the command guards, immutable limits, or admission
scanner.

The web UI is the compiled, checksummed Mainsail `v2.18.2` release artifact. It
is served from a read-only web root. Development TypeScript source is never used
as the production web root.

### Full mesh first, adaptive mesh later

The full `9x9` mesh costs time, but it measures the whole bed after it reaches
the print temperature. This is intentional while the stock bed's measured
range remains larger than one normal layer. Adaptive meshing becomes eligible
only after the physical bed is improved and repeated full meshes show that the
condition is gone.

The mesh compensates gradual height variation. It cannot repair loose parts,
probe noise, a sharply warped plate, incorrect flow, wet filament, or an
incorrect Z offset.

### GerGo remains private and attended

The purchased GerGo macro is never committed or quoted. Its exact installed
structure passed a read-only forbidden-command audit. The production config
cannot see it; the maintenance config is the only place that includes the
owner-supplied private file. A full v0.13 test still requires the original
purchased archive as a private staging input.

### Network, camera, and storage failures are secondary

Klipper, Moonraker, Crowsnest, Mainsail, KlipperScreen, the admission scanner,
Xorg, and the host MCU use separate service accounts and resource limits.
Klipper has priority; camera, UI, and scanner processes are lower priority.

Moonraker and Crowsnest listen only on loopback. Nginx is the sole remote
gateway and accepts only loopback plus the narrow direct-laptop IPv4 network
selected while staging. A future tablet network must be added as a separate,
explicitly reviewed trusted range; the candidate does not silently trust the
whole home LAN. SSH is normally off and can expose only a restricted, key-only
bundle receiver for one owner-gated deployment.

Timelapse asks for one non-parking frame per layer. Frames and videos live on
the removable USB, with frame, file-size, frame-count, free-space, render-time,
and video-size limits. Camera or USB failure disables timelapse without pausing
the print. Frames are removed only after the MP4 passes `ffprobe`; failed
renders retain their source frames. Journald and service log files have fixed
size ceilings.

The reusable Orca profile uses supported start, end, pause, filament-change,
object-label, exclude-object, print-sequence, power-loss, and
before-layer-change fields. It defaults to zero Z-hop and zero restart extra,
with a separate 0.4 mm collision-clearance machine variant. It does not
hand-edit a model or final G-code.

Orca pressure advance is stored per filament. During an admitted print the
safety extra accepts only `SET_PRESSURE_ADVANCE ADVANCE=value`, with a finite
value from 0 through 0.20. Smooth-time changes, alternate extruders, unknown
parameters, commands outside a protected print, and uploaded `TUNING_TOWER`
remain blocked. Startup, end, and cancel cleanup reset pressure advance to zero.
Klipper considers a paused virtual-SD job inactive, so a still-admitted paused
job may issue only `ADVANCE=0`; this lets cancellation finish safely without
opening paused printing to nonzero tuning changes.

## What this does not tune

The migration does not claim to fix stringing, filament ooze, surface quality,
or first-layer flow by itself. Those depend on measured temperature, maximum
volumetric speed, pressure advance, flow ratio, retraction, dry filament, and
the final Z offset. Old PID, mesh, Z offset, pressure advance, and input-shaper values are
not copied because they belong to the old software and physical state.

The earlier Frieren stringing can be helped by temperature and retraction
calibration after migration. The moving purge addresses the start blob near the
model, but it cannot stop filament from oozing while the nozzle is hot. The
powerline-network dropout is treated as a network/electrical issue, not a reason
to alter printer motion or heater behavior.

## Offline build commands

These commands only verify or construct laptop-side artifacts:

```bash
python3 bin/t300-mainline.py verify-cache
python3 bin/t300-provision.py \
  --stage PATH_TO_STAGE \
  --stage-manifest-sha256 MANIFEST_SHA256 \
  --verify-stage-only
python3 bin/test-klipper-v012.py
python3 bin/test-klipper-v013-mainline.py \
  --stage PATH_TO_STAGE \
  --stage-manifest-sha256 MANIFEST_SHA256
```

Do not use a stage merely because these commands pass. A commissioning stage
with bootstrap calibration is deliberately locked against motion, heat, steel
sheet arming, and printing.

## Recovery-media preflight

The recovery stick's kernel, initrd, boot scripts, root filesystem UUID, and
Klipad50 device tree are pinned in `stack.lock.json`. Audit the mounted boot
partition before every boot attempt:

```bash
python3 bin/t300-recovery-media.py audit --boot-root /path/to/armbi_boot
```

The audit must report `ready_for_interactive_usb_boot: true`. The stick found
during the 2026-08-06 preflight correctly matched every pinned payload hash but
lacked an explicit `fdtfile=rockchip/rk3328-mksklipad50.dtb` assignment. That
is unsafe with the stock screen's older U-Boot environment, so a corrected
laptop-local review file was rendered and the audit intentionally fails until
the owner approves copying it to the USB. Never replace the device tree by
guessing from the stock firmware name.

The owner-local recovery overlay has its own external manifest hash. Verify the
source before copying, then run the same read-only comparison against the
mounted recovery root after copying:

```bash
python3 bin/t300-recovery-media.py audit-overlay \
  --overlay-root PATH_TO_OVERLAY \
  --manifest-sha256 MANIFEST_SHA256
sudo python3 bin/t300-recovery-media.py audit-overlay \
  --overlay-root PATH_TO_OVERLAY \
  --manifest-sha256 MANIFEST_SHA256 \
  --installed-root /path/to/armbi_root
```

The second command needs read access to root-owned recovery files. Both audits
must report `ready: true`; neither command writes media.

The screen's service serial console runs at 1,500,000 baud, 8 data bits, no
parity, one stop bit, and no flow control. The repository helper is interactive
only:

```bash
python3 bin/t300-serial.py list
python3 bin/t300-serial.py console --device /dev/serial/by-id/EXACT_DEVICE
```

It validates the device path and permissions but sends no command. The owner
interrupts U-Boot and types its ordinary `run bootcmd_usb0` command. Do not use
kexec, intentionally damage eMMC bootability, or automate the boot command as
the primary recovery route.

The recovery root partition starts at about 2 GB and is expected to expand on
its first real USB boot through Armbian's enabled resize service. Confirm the
expanded filesystem and required free space before provisioning. A successful
Linux boot is not sufficient by itself: the recovery agent and laptop client
must both identify the target as fixed non-removable MMC with both eMMC boot
partitions, root must be on USB, eMMC must be unmounted, and three separate
eligible boots must be recorded before capture.

## Owner-gated migration order

1. Back up the existing printer USB and keep the private GerGo ZIP outside Git.
2. Create the pinned recovery USB, apply the reviewed boot-environment and
   recovery-overlay files with the owner present, remount read-only, and require
   the media audit above to pass.
3. Use the screen's service USB serial port to verify MKS-Klipad50 identity, root-on-USB, unmounted eMMC,
   bootloader handoff, and direct Ethernet. Record three separate successful
   USB boots. Stop immediately if the board, root disk, or eMMC differs.
4. Capture every eMMC sector over Ethernet. The tool performs a second raw
   device hash and checks the compressed stream.
5. On the laptop, perform the root-only filesystem verification:

   ```bash
   sudo python3 bin/t300ctl.py image verify \
     --image VENDOR_IMAGE.zst \
     --manifest VENDOR_IMAGE.zst.manifest.json \
     --filesystem-check \
     --workspace PRIVATE_WORKSPACE
   ```

   This materializes a temporary raw image, attaches it read-only, runs
   `fsck -n`, mounts every supported filesystem read-only, and detaches it.
6. Boot the signed Armbian USB and provision the exact stage locally. Provision
   from the screen or USB-C serial, never through SSH. Printer-control services
   remain disabled.
7. Run host-only validation: screen, touch, backlight, cooling fan, Ethernet,
   Wi-Fi, data USB, camera, storage, service permissions, and restart behavior.
8. Only after recovery and host validation, use Armbian's official interactive
   `armbian-install` from the local serial console to copy the validated live
   USB system to eMMC. The repository intentionally does not wrap or automate
   that disk writer. Confirm the exact eMMC target in the installer and stop if
   its detected layout is unexpected.
9. Boot eMMC with production still commissioning-locked. Establish MCU serial
   communication without commanding motion or heat.
10. Flash the STM32F401 and Linux host MCU only after the owner has verified the
    exact bootloader method and original recovery artifacts. The repository
    builds and hashes firmware but intentionally has no flash command.
11. Commission under attendance: sensor plausibility, endstop/probe states,
   fans, one axis at low speed, cleaned-and-rearmed homing with the sheet
   installed, one
    heater at a time, communication-loss shutdown, and Emergency Stop.
12. Recalibrate in maintenance mode: PID, extruder rotation distance, GerGo
    alignment, probe repeatability, Z offset, full mesh, ADXL input shaping,
    machine checks first, then filament temperature, maximum volumetric speed,
    pressure advance, flow ratio, and retraction in that order.
13. Deploy the reviewed calibration bundle, rerun exact validation, and unlock
    only through owner evidence tied to that configuration hash.
14. Print an unmodified standard first-layer test and a small dimensional part,
    then the orange camera bracket. Frieren comes only after those pass.
15. Render and verify a timelapse, soak camera/network behavior, and complete a
    real vendor rollback drill before final release acceptance.

## Rollback

Rollback is independent of the candidate. Boot the marked recovery USB, inspect
the same machine identity and unmounted eMMC, then restore only the matching
full-device capture:

```bash
python3 bin/t300ctl.py image write --help
```

The write path requires the recovery marker, three verified boots, exact
machine identity, exact disk size and sector geometry, an unmounted non-root
eMMC, `--apply`, and typed confirmation. It re-reads and hashes the complete
target after writing. Restore original MCU firmware too if an MCU had already
been changed, then verify stock 1.5.2 before any movement.

## Primary references

- [Official T300 support page](https://wiki.sovol3d.com/en/T300)
- [Official Armbian MKS-Klipad50 image page](https://www.armbian.com/mksklipad50/)
- [Official Armbian installation guide](https://docs.armbian.com/User-Guide_Getting-Started/)
- [MKS-Klipad50 USB boot procedure](https://torte71.github.io/InsideSovolKlipperScreen/booting.html)
- [Klipper configuration reference](https://www.klipper3d.org/Config_Reference.html)
- [KAMP source](https://github.com/kyleisah/Klipper-Adaptive-Meshing-Purging)
