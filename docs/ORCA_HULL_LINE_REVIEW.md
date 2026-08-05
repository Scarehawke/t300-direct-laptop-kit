# Orca hull- and floor-line review

## Decision

The project treats a localized horizontal line at a major cross-section change
as a model-and-process warning, not automatically as a printer defect. Future
valuable or large models must be reviewed in Orca Preview before final G-code
export. The review may recommend changes, but it must not apply geometry or
process changes without owner approval unless the diagnosis and harmless fix
are exceptionally clear.

This decision follows a timestamped transcription of Factorian Designs' video
`The Most Misdiagnosed Print Error - Fix ALL Shrink & Wall Lines`, published
2025-11-21:

https://www.youtube.com/watch?v=ITighzYPTTs

The video credits OrcaSlicer pull request 8107 and Prusa's Benchy hull-line
investigation. Those primary sources were checked rather than treating the
video as authority by itself:

- https://github.com/OrcaSlicer/OrcaSlicer/pull/8107
- https://help.prusa3d.com/article/the-benchy-hull-line_124745
- https://github.com/OrcaSlicer/OrcaSlicer/wiki/quality_settings_wall_and_surfaces
- https://github.com/OrcaSlicer/OrcaSlicer/wiki/quality_settings_precision
- https://github.com/OrcaSlicer/OrcaSlicer/wiki/strength_settings_infill

## What is well supported

- A broad solid floor or deck connected to a thin continuing wall can create a
  localized horizontal line as the material cools, contracts and pulls on the
  wall. A sparse-to-solid transition is another common trigger.
- Cooling, material, environment, path geometry and the mechanical coupling
  between solid regions and walls all influence severity. There is no universal
  correction that is reliable for every printer, material and shape.
- A defect confined to the height of a clear geometry transition is usually not
  evidence of a damaged Z axis. Repeated irregular layers throughout the whole
  object remain a machine-diagnostic signal.
- Layer-time view is useful for locating a transition, but smoothing layer time
  is not an established correction. Orca pull request 8107 was closed after
  extensive testing because the hull line persisted.
- For PLA, strong and even part cooling, slower solid infill, Inner/Outer/Inner
  wall order where geometry allows it, Precise Wall, and less wall overlap can
  help. Every one remains model, material and profile dependent.
- Orca documents roughly 10-15 percent infill/wall overlap as a range that can
  reduce material accumulation while retaining normal bonding. Reducing it
  further can weaken the part.

## Advisory review gate

Warn the owner when the sliced model shows one or more of these conditions:

1. A wide solid base, shelf or deck abruptly becomes a much thinner wall.
2. Sparse infill abruptly changes to several nearly solid layers while the same
   external wall continues above and below the transition.
3. A large connected solid region terminates against a thin cosmetic wall.
4. Preview shows a sharp feature-flow or cross-section transition at the same Z
   height where surface quality matters.

The warning should identify the exact height and explain that the artifact is a
risk, not a certainty. Review, in this order:

1. Confirm the selected filament's cooling behavior and calibration.
2. Compare feature type, flow and layer-time Preview at the transition.
3. Check resolved solid-infill speed and pattern, wall order, Precise Wall, and
   sparse and top/bottom wall-overlap values.
4. Prefer a small test section when the print is long or expensive.
5. If design access exists, offer chamfers, fillets, ribs, segmented ledges or
   other decoupling changes as owner-directed CAD options.

Do not automatically reduce overlap, change wall order, add walls, disable
vertical shell thickness, change cooling, apply fuzzy skin, or modify the CAD.
Those choices can alter strength, dimensions, overhang support, seams, surface
character or warping behavior. Do not manually edit final G-code or meshes.

## Current staged projects

### Orange camera bracket

Warning warranted. Its broad base-to-upright transition resembles the classic
box/floor-line geometry. The exact Orca 2.4.2 validation slice resolves to 25
percent infill/wall and top/bottom wall overlap, Inner/Outer wall order, Precise
Wall off, monotonic internal solid infill and full PLA fan availability. The
25-percent overlap is above Orca's documented 10-15 percent accumulation-aware
guidance.

The resolved feature paths make the warning concrete: at Z 1.20 mm, connected
solid extrusion drops from about 411.5 mm of filament-equivalent paths on the
preceding layer to 14.9 mm while roughly 33.1 mm of outer-wall extrusion
continues. This is a strong risk signal, but not proof that a visible line will
occur on this printer and filament.

No setting has been changed. Before export, inspect the transition in Preview
and decide whether strength-first defaults or a small overlap/wall-order test is
appropriate. The bracket remains a useful low-cost validation print.

### Frieren

No automatic change warranted. The figure has many changing cross-sections,
but curved and irregular exterior surfaces are less likely to show one long,
straight hull line than a box or tray. Preview should still be checked for
large support-contact floors, broad nearly solid sections and abrupt feature
flow changes. The existing quality and easier-release projects remain editable.

## Scope

This review rule addresses local shrink and wall-coupling artifacts. It does not
replace filament temperature, flow, pressure-advance, retraction or cooling
calibration, and it does not diagnose model-wide stringing.
