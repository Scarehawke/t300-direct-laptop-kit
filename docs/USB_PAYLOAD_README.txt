T300 PREPARED FILES - READ THIS FIRST

These G-code files require the tested t300_core.cfg printer overlay. Do not run
any of them while the printer still uses the old factory START_PRINT behavior.
The Mainsail console must first report the expected result from:

    T_CORE_STATUS

Before every print, physically confirm that the removable metal build sheet is
installed and flat, the nozzle is clean, and the motion path is clear. The
stock inductive probe cannot detect Z safely when the metal sheet is absent.

NEXT PRINT, WITH THE OWNER PRESENT:

    01-NEXT-FIRST-LAYER/T300_PLA_FIRST_LAYER_CENTER_215C.gcode

Run it with no temporary G-code Z offset. Expected startup: heat bed, home,
probe a full 9x9 mesh with the nozzle cold, raise Z before XY, heat nozzle,
draw two moving purge lines, then print the patch. Stop for scraping, open or
detached lines, a nozzle blob, unexpected movement, or power instability.

LATER, ONLY AFTER REVIEW OF THE CENTER PATCH:

    02-AFTER-CENTER/T300_PLA_FIRST_LAYER_FIVE_POINT_215C.gcode
    02-AFTER-CENTER/T300_PLA_TEMP_STRING_220-195C.gcode

The tower intentionally visits 220 C as its hottest test section. Normal first
layers now use 215 C.

NOT AUTHORIZED FOR PRINTING:

    99-DO-NOT-PRINT/T300_FRIEREN_NATIVE_012_PROVISIONAL_DO_NOT_PRINT.gcode

Frieren is an updated planning slice, not the final print. Temperature,
pressure advance, overall flow, retraction, and support contacts must be
accepted first.

Use SHA256SUMS to verify that the files were copied without corruption.
