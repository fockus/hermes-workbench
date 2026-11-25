---
name: productivity-workspace-automation
description: "Use when automating productivity and knowledge-work systems: Google Workspace, Airtable, Notion, PDFs/OCR, PowerPoint, maps/geocoding, Obsidian notes, email, and meeting pipelines."
version: 1.0.0
author: Hermes Workbench contributors
license: MIT
metadata:
  hermes:
    tags: [productivity, google-workspace, airtable, notion, pdf, powerpoint, maps, email, obsidian]
    related_skills: []
---

# Productivity Workspace Automation

## Overview

This umbrella covers structured personal/work data and document operations: cloud suites, databases, notes, PDFs, presentations, maps, email, and meeting pipeline workflows.

## When to Use

- Gmail/Calendar/Drive/Docs/Sheets through Google Workspace tooling.
- Airtable records, filters, CRUD, and upserts.
- Notion pages, databases, blocks, markdown, and API operations.
- PDF text edits, OCR extraction, document conversion, and PowerPoint decks.
- Maps/geocoding/route/timezone lookups.
- Obsidian vault notes and email via CLI.
- Teams meeting summary pipeline inspection or replay.

## Operating Pattern

1. Identify the workspace system and credential/tooling requirement.
2. Search/read before creating duplicates.
3. Make the smallest safe mutation.
4. Read back the created/updated record, note, file, or pipeline status.
5. Report stable handles: document URL, record id, file path, or message id.

## Sub-workflows

### Tables and databases
Airtable, Sheets, and Notion databases need schema discovery before writes.

### Documents and decks
For PDFs, OCR, and PowerPoint, preserve formatting where possible and verify exported/opened output.

### Mail, calendar, and meetings
Treat communications as side-effectful: resolve recipients, dates, timezones, and threads before sending/updating.

### Maps and locations
Use geocoding/route tools and include coordinates/timezone/route source when relevant.

### Notes and knowledge vaults
Search before creating, use clear titles, and verify note contents after edits.

## Common Pitfalls

1. Do not mutate external systems without confirming ambiguous targets.
2. Do not assume schema; inspect fields/columns first.
3. Do not claim a document edit succeeded until the output file or object is read back.

## Verification Checklist

- [ ] Tooling/credentials available.
- [ ] Target object/schema identified.
- [ ] Mutation or extraction was verified by read-back.
