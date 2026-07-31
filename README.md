# T300 Direct Laptop Kit

Connect an Arch Linux laptop directly to a Sovol/Comgrow T300 over Ethernet
while the laptop keeps its normal Internet connection over Wi-Fi. No router and
no Internet connection on the printer are required.

The kit also provides bounded, read-only configuration backups and a
conservative installer for a **user-supplied** GerGo T300 Z-tilt-via-knob v3
macro.

## What you need

- An Arch/EndeavourOS-style laptop using NetworkManager.
- One ordinary Ethernet cable. A crossover cable is not required.
- Python 3 and `nmcli` (provided by the `networkmanager` package).
- A powered-on T300 that is idle and running its normal Klipper firmware.
- For the GerGo workflow: the ZIP purchased from the creator's
  [Cults page](https://cults3d.com/en/3d-model/tool/z-tilt-via-knob-macro-models-on-comgrow-t300).

This repository intentionally does not redistribute GerGo's paid macro or STL
files.

## Direct connection: quick path

Keep the laptop connected to the Internet over Wi-Fi. Connect its Ethernet port
directly to the T300's RJ45 port, then run:

```bash
git clone https://github.com/Scarehawke/t300-direct-laptop-kit.git
cd t300-direct-laptop-kit
./bin/t300-link interfaces
./bin/t300-link up
```

NetworkManager creates a private `10.42.42.0/24` network, supplies DHCP to the
printer, and marks the Ethernet connection as never-default so it does not take
over the laptop's Wi-Fi route.

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

## GerGo macro: dry run, then installation

Download the purchased archive to the laptop. First perform a dry run:

```bash
python3 ./bin/t300ctl.py install-gergo \
  --host 10.42.42.x \
  --source "$HOME/Downloads/macro_v3(extract!).zip"
```

The exact archive name may differ. The helper searches the archive for
`macro_z_tilt_via_knob.cfg`, validates it as a small UTF-8 Klipper
configuration, and shows the proposed `printer.cfg` diff. It makes no changes
without `--apply`.

After reviewing the output, install it:

```bash
python3 ./bin/t300ctl.py install-gergo \
  --host 10.42.42.x \
  --source "$HOME/Downloads/macro_v3(extract!).zip" \
  --apply
```

The apply operation:

1. Verifies that Klipper is ready and not printing or paused.
2. Refuses to overwrite a different existing macro file.
3. Downloads a complete configuration backup.
4. Uploads the macro.
5. Adds `[include macro_z_tilt_via_knob.cfg]` immediately after the existing
   `[include Macro.cfg]` line.
6. Restarts Klipper and waits for it to report ready.
7. Automatically restores the original `printer.cfg` if the new configuration
   fails to load.

The separate macro file may remain uploaded after an automatic rollback, but it
is inert because the restored `printer.cfg` does not include it.

## First macro test

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

This deactivates the private connection without deleting the saved profile.
Your Wi-Fi connection is not modified.

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

The scripts and original documentation in this repository use the MIT License.
Third-party macro and model files retain their respective authors' licenses and
are not included.
