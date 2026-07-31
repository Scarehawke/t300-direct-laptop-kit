# T300 laptop handoff

This repository is the public, MIT-licensed part of the owner's Comgrow/Sovol
T300 setup. The owner has separately purchased GerGo Print 3D's T300
z-tilt-via-knob package. That licensed package must never be committed, uploaded
to a public release, quoted in project documentation, or obtained from an
unofficial mirror.

## Private macro handoff

The untouched Cults download has been placed on the owner's printer USB at this
relative path:

```text
T300-Laptop-Private/z-tilt-via-knob-macro-models-on-comgrow-t30020250415-1-3jsx6.zip
```

Its expected SHA-256 is:

```text
c4af725ece0ccb4cc2757fbe8d76150a5018a0c0a6a9c7715c5461b8ad5ab64e
```

The USB volume is FAT32, currently identified as `/dev/sdc1`, with filesystem
UUID `C66C-ADD5` and no label on the desktop. Device names are not stable: on
the laptop, use
`lsblk -o NAME,PATH,TRAN,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS` and locate the
removable volume rather than assuming `/dev/sdc1`. Typical desktop mounts are
under `/run/media/$USER/C66C-ADD5/`.

If the archive is absent, ask the owner to reconnect the USB or download it
again from their Cults account. Do not substitute a third-party copy.

The archive contains another archive named `macro_v3(extract!).zip`; the
project helper intentionally handles both layers. Do not add either archive or
the extracted `macro_z_tilt_via_knob.cfg` to this Git repository. The ignore
rules are deliberate.

## Intended installation flow

The printer remains on Sovol/Comgrow firmware 1.5.2, which embeds Klipper
0.12.0. Do not run generic Klipper/Moonraker updates from Mainsail.

1. Keep the laptop online over Wi-Fi and connect its Ethernet port directly to
   the T300.
2. Run `./bin/t300-link up`, then `python3 ./bin/t300ctl.py discover`.
3. Run `python3 ./bin/t300ctl.py check --host PRINTER_IP`.
4. Copy the purchased outer ZIP from the USB to a private location such as the
   laptop's Downloads directory and verify its SHA-256 above.
5. Perform the GerGo dry run first:

   ```bash
   python3 ./bin/t300ctl.py install-gergo \
     --host PRINTER_IP \
     --source "/private/path/to/z-tilt-via-knob-macro-models-on-comgrow-t30020250415-1-3jsx6.zip"
   ```

6. Review the macro section names and proposed `printer.cfg` include. Only then
   repeat with `--apply`. The apply path creates a complete configuration
   backup and has automatic `printer.cfg` rollback.
7. Keep the bed clear and the owner beside the printer. Test
   `Z_TILT_CALIBRATION` from the Mainsail console before trying the touchscreen
   Z-tilt button.
8. The purchased v3 file defaults to its safer `quick: 0` mode and faster
   `accuracy: 1` mode. After a successful baseline test, the creator documents
   changing `accuracy` to `0` for tighter results at the cost of more cycles.

Do not install the repository's open `GANTRY_LEVEL_T300` alternative alongside
GerGo's macro. The owner has chosen the purchased GerGo v3 workflow. Never copy
the included reference `printer.cfg` or reference configuration bundle over the
printer wholesale; GerGo explicitly labels those files reference-only.

## Validation already completed

- The exact purchased macro was extracted privately and its three macro
  sections were recognized.
- A full right-probe, left-probe, result cycle completed in the Klipper 0.12.0
  regression harness using the stock T300 axis limits and probe offsets.
- `bin/t300ctl.py` accepts the complete official outer Cults ZIP as of commit
  `ab11adb`.
- The repository unit tests and GitHub Actions were passing at that commit.
