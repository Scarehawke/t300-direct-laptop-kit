"""Compatibility alias for running 2023 Klipper on the laptop's Python 3.14."""

import configparser


if not hasattr(configparser.RawConfigParser, "readfp"):
    configparser.RawConfigParser.readfp = configparser.RawConfigParser.read_file
