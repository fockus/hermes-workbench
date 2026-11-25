---
name: creative-visual-artifacts
description: "Use when creating visual or interactive artifacts: diagrams, HTML mockups, infographics, design-system inspired pages, Excalidraw scenes, p5.js sketches, and DESIGN.md tokens."
version: 1.0.0
author: Hermes Workbench contributors
license: MIT
metadata:
  hermes:
    tags: [creative, diagrams, html, design, infographics, excalidraw, p5js]
    related_skills: []
---

# Creative Visual Artifacts

## Overview

This umbrella covers static and browser-rendered visual artifacts. Prefer one coherent artifact workflow with labeled branches over loading many narrow design skills.

## When to Use

- Architecture/cloud/infra diagrams as HTML/SVG.
- Hand-drawn Excalidraw diagrams and flow/sequence scenes.
- One-off HTML landing pages, mockups, prototypes, and design variants.
- Infographics and structured visual explanations.
- p5.js generative art, shaders, interaction, and WebGL sketches.
- DESIGN.md token specs and design-system inspired page treatments.
- Text-layout browser demos and kinetic typography.

## Artifact Branches

### Diagrams
Use HTML/SVG for polished dark architecture diagrams; use Excalidraw JSON for hand-drawn collaborative diagrams. Verify readability at the target export size.

### HTML mockups and prototypes
For product screens, landing pages, decks, and design variants, produce self-contained HTML/CSS/JS. Offer 2-3 variants when exploration is the goal.

### Infographics
Convert raw information into a layout, visual hierarchy, labels, and concise copy. Select style and layout intentionally rather than dumping content.

### Design systems and tokens
Use DESIGN.md or named design-system references when the user wants reusable visual language. Keep tokens, components, and examples aligned.

### Generative and typographic sketches
Use p5.js or text-layout libraries for interactive/animated browser art. Keep export paths and browser testing explicit.

## Common Pitfalls

1. Do not create separate one-off skills for each aesthetic; put styles as subsections or templates.
2. Visual artifacts require visual inspection, not just file creation.
3. Browser demos should be single-file unless the user requested a project structure.

## Verification Checklist

- [ ] Output artifact path or URL exists.
- [ ] Browser or image rendering was inspected.
- [ ] The chosen style/layout matches the user's stated intent.
