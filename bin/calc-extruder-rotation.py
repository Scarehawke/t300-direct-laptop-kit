#!/usr/bin/env python3
"""Calculate a Klipper extruder rotation distance from measured filament travel."""

from __future__ import annotations

import argparse
import math


def positive(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive finite number")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initial", required=True, type=positive, help="initial mark distance in mm")
    parser.add_argument("--final", required=True, type=positive, help="remaining mark distance in mm")
    parser.add_argument("--requested", default=50.0, type=positive, help="commanded extrusion in mm")
    parser.add_argument("--current", default=3.55, type=positive, help="current rotation_distance")
    args = parser.parse_args()

    actual = args.initial - args.final
    if actual <= 0:
        parser.error("initial distance must be greater than final distance")
    new_distance = args.current * actual / args.requested
    deviation = actual - args.requested
    percent = deviation / args.requested * 100

    print(f"Requested extrusion: {args.requested:.3f} mm")
    print(f"Actual extrusion:    {actual:.3f} mm")
    print(f"Deviation:           {deviation:+.3f} mm ({percent:+.2f}%)")
    print(f"Current distance:    {args.current:.5f}")
    print(f"Calculated distance: {new_distance:.5f}")
    print(f"Klipper value:       {new_distance:.3f}")
    print(
        "Temporary command:  SET_EXTRUDER_ROTATION_DISTANCE "
        f"EXTRUDER=extruder DISTANCE={new_distance:.3f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
