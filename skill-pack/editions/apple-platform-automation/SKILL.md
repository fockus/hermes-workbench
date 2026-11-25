---
name: apple-platform-automation
description: "Use when operating Apple/macOS user-facing apps and devices from Hermes: Notes, Reminders, Messages, Find My, and background desktop computer-use."
version: 1.0.0
author: Hermes Workbench contributors
license: MIT
metadata:
  hermes:
    tags: [apple, macos, notes, reminders, messages, findmy, computer-use]
    related_skills: []
---

# Apple Platform Automation

## Overview

This is the umbrella workflow for Apple/macOS automations. Load it when the task involves local Apple apps, iCloud-adjacent personal data, device location, Messages/SMS, or driving the macOS GUI in the background.

## When to Use

- Create, search, or edit Apple Notes.
- Add, list, or complete Apple Reminders.
- Send or inspect iMessage/SMS conversations.
- Locate Apple devices or AirTags through Find My.
- Operate the macOS desktop without stealing focus from the user.

## Sub-workflows

### Notes
Use the memo CLI pattern: search before creating duplicates, quote exact note titles, and verify writes by reading the note back.

### Reminders
Use remindctl-style list/add/complete flows. Prefer explicit list names and due dates; verify completion state after changes.

### Messages
Treat Messages as user-visible communication. Confirm recipient identity when ambiguous, avoid exposing private thread content, and verify send status from the CLI.

### Find My
Use Find My for device/AirTag location checks. Report timestamps and uncertainty; never overstate live location precision.

### macOS computer use
Use background screenshots, mouse, keyboard, scroll, and drag when CLI/API access is unavailable. Verify UI state after each important action. If a pinned CuaDriver bundle exists but the MCP session cannot launch it by name, run the supported `hermes computer-use install` to register that signed bundle with LaunchServices, then `hermes computer-use doctor`. An active MCP session does not grant Accessibility or Screen Recording; the user grants those to CuaDriver in macOS. Do not reset permissions or trust an entire Homebrew tap just to get one formula; review and approve formula-specific trust if Homebrew refuses an unsigned or untrusted source.

Use the host catalog to find detailed command references; availability varies by installation.

## Common Pitfalls

1. Do not confuse the user's personal macOS with a remote Linux shell.
2. Do not send messages or modify personal data when the recipient/list/note is ambiguous.
3. GUI automation requires visual verification; never assume a click succeeded. If a permissions probe is green but the app-scoped window lookup returns AX error `-25211`, treat input as unverified in that caller. Do not use an earlier window ID or the frontmost app as a substitute. A prior screenshot proves capture, not input.

4. A freshly launched Calculator can restore a previous calculation. Before a test input, verify the display in the identified window, not the numeric keypad labels in an interactable-only tree; if there is an existing calculation and no clean window, stop without clicking or clearing.

## Verification Checklist

- [ ] The intended Apple app or CLI was available.
- [ ] Ambiguous personal-data targets were resolved before side effects.
- [ ] The final state was read back or visually verified.
