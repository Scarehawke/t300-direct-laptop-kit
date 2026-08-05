# T300 Direct Laptop Kit

Connect an Arch Linux laptop directly to a Sovol/Comgrow T300 over Ethernet
while the laptop keeps its normal Internet connection over Wi-Fi. No router is
required.

The kit also provides bounded, read-only configuration backups and includes an
original, MIT-licensed `GANTRY_LEVEL_T300` macro. The macro measures the gantry
with the inductive probe and reports how far to turn the stock right Z knob. It
does not use the T300's inaccurate top-ramming routine.

## Current printer and mainline status

The owner's printer is still running the restorable Comgrow **1.5.2** image and
its patched Klipper **0.12.0**. The `mainline/` and `t300_mainline/` trees are a
local, commissioning-locked Klipper **v0.13.0** candidate. They have not been
written to eMMC, have not flashed either MCU, and are not approved for an
unattended print.

Read [the mainline migration guide](docs/MAINLINE_MIGRATION.md) before using any
mainline command. Its implementation and remaining physical gates are recorded
in [the mainline audit](docs/MAINLINE_IMPLEMENTATION_AUDIT_20260804.md). The
latest restart point is [the 2026-08-05 handover](docs/HANDOVER_20260805.md).
The matching OrcaSlicer contract is documented in
[the runtime profile notes](docs/ORCASLICER_RUNTIME_PROFILE.md).
The owner-facing sign-off report lives in the data-only
[T300 Dossier project](dossier/README.md).
Read it before continuing after a chat reset, reboot, or context compaction. The
candidate deliberately provides no automatic eMMC or MCU flashing command;
those owner-attended operations remain blocked until recovery and hardware
identity have been proven on the actual screen.

## What you need

- An Arch/EndeavourOS-style laptop using NetworkManager.
- One ordinary Ethernet cable. A crossover cable is not required.
- Python 3 and `nmcli` (provided by the `networkmanager` package).
- A powered-on T300 that is idle and running its normal Klipper firmware.

## Direct connection: quick path

Keep the laptop connected to the Internet over Wi-Fi. Connect its Ethernet port
directly to the T300's RJ45 port, then run:

```bash
git clone https://github.com/Scarehawke/t300-direct-laptop-kit.git
cd t300-direct-laptop-kit
./bin/t300-link interfaces
./bin/t300-link up
```

NetworkManager creates a direct `10.42.42.0/24` network, supplies DHCP to the
printer, and marks the Ethernet connection as never-default so it does not take
over the laptop's Wi-Fi route. Its `ipv4.method shared` mode also enables IP
forwarding and NAT to the laptop's default connection. The printer can therefore
reach the Internet through Wi-Fi; this helper is convenient connectivity, not
an isolation or firewall boundary.

On the printer, open **Advanced → Show IP**. It should show an address beginning
with `10.42.42.`. If it does, open the address in a browser:

```text
http://10.42.42.x
```

That is the printer's local Mainsail interface. You can also let the helper find
it:

```bash
python3 ./bin/t300ctl.py discover
```

If the laptop has more than one unused Ethernet interface, specify the desired
one explicitly:

```bash
./bin/t300-link up --interface enp3s0
```

## Inspect and back up first

Replace `10.42.42.x` with the printer address:

```bash
python3 ./bin/t300ctl.py check --host 10.42.42.x
python3 ./bin/t300ctl.py backup --host 10.42.42.x
```

The backup is written beneath `t300-backups/`. The untouched files from
Moonraker's `config` root are kept below `config-root/`, alongside a manifest
and SHA-256 checksums. The backup process has per-file and total-size caps and
will abort rather than ingest unexpectedly large data.

Do not use Mainsail's update manager to update the vendor Klipper installation.
The T300 screen and factory macros depend on Sovol/Comgrow's customized image.
Sovol firmware **1.5.2** is the complete vendor release; it contains Klipper
**0.12.0** as one component. Those two version numbers are not alternatives.

## Install the included open leveling macro

**Owner-specific warning:** do not install this alternative on the current
printer. The purchased GerGo v3 workflow is already selected and must remain
the only gantry-leveling owner. This section is retained for other repository
users who have not installed GerGo.

The installer reads the T300's live `bed_mesh`, probe offsets, axis limits, and
Z `rotation_distance`. It calculates and displays safe probe/nozzle positions
before offering to upload anything. No other owner's hard-coded coordinates are
used.

First perform a dry run:

```bash
python3 ./bin/t300ctl.py install-open-level --host 10.42.42.x
```

Review the calculated geometry and proposed one-line `printer.cfg` change. Then
apply it:

```bash
python3 ./bin/t300ctl.py install-open-level --host 10.42.42.x --apply
```

The apply operation:

1. Verifies that Klipper is ready and not printing or paused.
2. Validates the live mesh, probe, axis, and Z-screw configuration.
3. Refuses to overwrite a different existing macro file or coexist with the
   selected GerGo include.
4. Downloads a complete configuration backup.
5. Uploads `t300_gantry_level.cfg`.
6. Adds `[include t300_gantry_level.cfg]` immediately after the existing
   `[include Macro.cfg]` line.
7. Restarts Klipper and waits for it to report ready.
8. Automatically restores the original `printer.cfg` if the new configuration
   fails to load.

### First leveling run

Keep the bed clear and remain beside the printer. In Mainsail's console, first
check that the probe is repeatable:

```text
PROBE_ACCURACY SAMPLES=10
```

Do not trust tilt measurements if the reported probe range is above 0.025 mm.
If repeatability is acceptable, run:

```text
GANTRY_LEVEL_T300 BED_TEMP=60 TOLERANCE=0.02
```

The macro heats the bed, homes, probes right and then left three times each, and
reports one of these outcomes:

- **PASS:** the difference is within 0.02 mm. It re-homes and keeps the motors
  enabled so a bed mesh can follow.
- **RIGHT SIDE IS LOW/HIGH:** it raises the head, disables the Z driver, and
  reports the required movement in millimetres, degrees, and clock-minutes.
  Turn the stock right knob in the direction that raises or lowers that side,
  then run the macro again. Every new run re-homes first.

Once it passes, run the printer's normal bed-mesh calibration and inspect the
mesh before printing. The open macro intentionally does not replace a factory
touchscreen button; run it from Mainsail until the behavior has been validated.

## Optional GerGo v3 installer

The original GerGo v3 knob, dial, and macro package is a separate optional
alternative. This repository does not redistribute those third-party files. If
you acquire the archive from the creator's
[Cults page](https://cults3d.com/en/3d-model/tool/z-tilt-via-knob-macro-models-on-comgrow-t300),
the kit can install it with the same backup and rollback protections.

Dry run:

```bash
python3 ./bin/t300ctl.py install-gergo \
  --host 10.42.42.x \
  --source "$HOME/Downloads/YOUR-CULTS-DOWNLOAD.zip"
```

The exact archive name may differ. You can supply the complete ZIP downloaded
from Cults, the nested `macro_v3(extract!).zip`, or the extracted CFG. The helper
finds `macro_z_tilt_via_knob.cfg`, validates both archive layers and the macro as
a small UTF-8 Klipper configuration, and shows the proposed `printer.cfg` diff.
It refuses an active open-gantry include and makes no changes without `--apply`.

After reviewing the output, install it:

```bash
python3 ./bin/t300ctl.py install-gergo \
  --host 10.42.42.x \
  --source "$HOME/Downloads/YOUR-CULTS-DOWNLOAD.zip" \
  --apply
```

Uploads are verified byte for byte. If any upload, restart, or readiness check
fails, the helper restores every changed file and removes any file newly created
by the failed transaction. It restarts again only if a Klipper reload had
already been attempted; disk-only staging failures are restored without an
unnecessary printer interruption.

### GerGo touchscreen test

Do not assume the touchscreen shortcut has been replaced successfully. Firmware
1.5.2 owners have reported differences in factory macro naming and include
behavior.

1. Leave the printer unloaded and the bed clear.
2. Open Mainsail on the laptop.
3. Confirm the printer reports **Ready**.
4. Inspect the uploaded macro's comments and macro names.
5. Run the documented primary macro from Mainsail's console first.
6. Remain beside the printer with a hand near its power switch during the first
   homing/probing cycle.
7. Use the stock right Z knob with a temporary marker line until the printed
   knob and dial are available.
8. Only test the touchscreen Z-tilt button after the console test succeeds.

Never paste GerGo's complete reference `printer.cfg` onto the printer. Its own
description labels that bundle reference-only.

## Disconnecting

```bash
./bin/t300-link down
```

This deactivates the direct connection without deleting the saved profile.
Your Wi-Fi connection is not modified.

## Optional KAMP park and purge

The approved KAMP integration keeps the T300 native adaptive mesher and
installs only Smart Park and Line Purge. Read
[`docs/KAMP_T300_INTEGRATION.md`](docs/KAMP_T300_INTEGRATION.md) before use.
Perform a dry run first, then apply only while the printer is idle:

```bash
python3 ./bin/t300ctl.py install-kamp-subset --host PRINTER_IP
python3 ./bin/t300ctl.py install-kamp-subset --host PRINTER_IP --apply
```

## Optional laptop camera recording

The printer's layer timelapse and a continuous laptop recording are separate.
To record the existing MJPEG stream on the laptop with reconnect handling and a
space-conscious default, run:

```bash
./bin/record-t300-camera.sh --host PRINTER_IP --duration 30m
```

Recordings default to `.cache/camera-recordings/` as interruption-tolerant
Matroska files. A browser preview plus this recorder are two simultaneous stream
clients and can add load to the printer and network. Prefer the built-in
non-parking layer timelapse for normal prints and use continuous recording as an
optional diagnostic observer.

## Community macro research

Live changes are governed by
[docs/CHANGE_POLICY.md](docs/CHANGE_POLICY.md). In short: T300-specific
community evidence, compatibility review, and explicit owner approval are all
required before a macro or configuration change reaches the printer.

See [docs/COMMUNITY_STACK_RESEARCH.md](docs/COMMUNITY_STACK_RESEARCH.md) for the
stock-hardware macro and extension evaluation, safety findings, and proposed
phased T300 package architecture.

The earlier quarantined implementation and its historical hookup sequence are
in [docs/PREPARED_T300_UPGRADE.md](docs/PREPARED_T300_UPGRADE.md). The locally
authored core overlay is quarantined and must not be applied or used by a print.

The preliminary whole-stack review remains in
[docs/PRELIMINARY_RUNTIME_DESIGN_20260804.md](docs/PRELIMINARY_RUNTIME_DESIGN_20260804.md),
with its earlier findings in
[docs/PRELIMINARY_IMPLEMENTATION_AUDIT_20260804.md](docs/PRELIMINARY_IMPLEMENTATION_AUDIT_20260804.md).
The newer mainline guide and audit supersede those documents for migration
status. Nothing from the mainline candidate has been sent to the printer, and
no generated G-code from it is approved to print yet.

## Authentication

Most vendor T300 installations trust clients on the local network. If your
Moonraker installation requires an API key, keep the key out of shell history:

```bash
read -rsp 'Moonraker API key: ' T300_KEY; export T300_KEY; echo
python3 ./bin/t300ctl.py check --host 10.42.42.x --api-key-env T300_KEY
unset T300_KEY
```

Add `--api-key-env T300_KEY` to the backup or install command as well.

## References

- [NetworkManager shared IPv4 mode](https://www.networkmanager.dev/docs/api/latest/nm-settings-nmcli.html)
- [Moonraker file-management API](https://moonraker.readthedocs.io/en/latest/external_api/file_manager/)
- [Moonraker printer-administration API](https://moonraker.readthedocs.io/en/latest/external_api/printer/)
- [Official Sovol T300 support page](https://wiki.sovol3d.com/en/T300)
- [GerGo T300 macro and knob package](https://cults3d.com/en/3d-model/tool/z-tilt-via-knob-macro-models-on-comgrow-t300)

## License

The scripts, `GANTRY_LEVEL_T300` macro, and original documentation in this
repository use the MIT License. Third-party macro and model files retain their
respective authors' licenses and are not included.
