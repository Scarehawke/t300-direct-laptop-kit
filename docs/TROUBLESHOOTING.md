# Troubleshooting and recovery

## The T300 shows no Ethernet IP

1. Confirm both ends of the Ethernet cable are fully seated.
2. Run `./bin/t300-link status`.
3. Run `./bin/t300-link down`, then `./bin/t300-link up`.
4. Wait 20 seconds and reopen **Advanced → Show IP** on the T300.
5. Run `python3 ./bin/t300ctl.py discover`.

The direct-link profile uses `10.42.42.1/24`. The printer should receive another
address in that subnet from NetworkManager's DHCP service.

## `nmcli` reports insufficient privileges

Run the same command from a normal graphical login session so NetworkManager's
PolicyKit agent can authorize it. Do not run the entire kit as root. If the
laptop intentionally lacks a PolicyKit agent, only the network command may be
run with `sudo`:

```bash
sudo ./bin/t300-link up --interface YOUR_ETHERNET_INTERFACE
```

Run `t300ctl.py` as the normal user so backups remain user-owned.

## Mainsail opens, but the helper reports HTTP 401

Moonraker authorization is enabled. Follow the API-key environment-variable
example in the main README. Do not put an API key in a Git repository or pass it
directly on a command line.

## Klipper rejects the macro

The installer automatically attempts to restore the original `printer.cfg`.
The printed backup path contains:

- the untouched original `config-root/printer.cfg`;
- every other file exposed by the config root;
- `proposed-printer.cfg`;
- checksums and a manifest.

If automatic rollback cannot complete, open Mainsail's **Machine** page, upload
the backed-up `printer.cfg`, and use **Firmware Restart**. Do not factory-reset
or reflash the printer merely for a configuration syntax error.

## Klipper is ready, but the touchscreen Z-tilt button behaves as before

Do not paste the macro into `Macro.cfg` immediately. First run the macro from
Mainsail's console and compare the current firmware 1.5.2 `Macro.cfg` definitions
with the purchased macro. The touchscreen can be wired to a factory macro name
that differs between firmware revisions. Resolve that mapping only after the
probing behavior itself has been verified.

## Returning the laptop to normal

Run:

```bash
./bin/t300-link down
```

The profile has `connection.autoconnect=no`, so it will not silently reconnect
after a reboot. It does not alter or remove any Wi-Fi profile.
