---
name: github-operations
description: "Use when operating GitHub end-to-end: authentication, repositories, issues, pull requests, code review, CI triage, releases, and gh/API workflows."
version: 1.0.0
author: Hermes Workbench contributors
license: MIT
metadata:
  hermes:
    tags: [github, gh, git, pull-requests, issues, code-review, ci]
    related_skills: [codebase-inspection]
---

# GitHub Operations

## Overview

This umbrella covers GitHub work as a lifecycle: authenticate, inspect or create repositories, manage issues, branch and open PRs, review diffs, monitor CI, merge or release, and leave auditable evidence.

## When to Use

- Setting up `gh` authentication, HTTPS tokens, or SSH keys.
- Creating, cloning, forking, or configuring repositories and remotes.
- Creating, triaging, labeling, assigning, or closing issues.
- Building a branch, committing, opening/updating PRs, watching CI, and merging.
- Reviewing PRs, writing inline comments, or producing review summaries.

## Workflow

1. **Discover context:** `git status`, `git remote -v`, `gh auth status`, and repo metadata.
2. **Choose the GitHub object:** repo, issue, PR, release, or workflow run.
3. **Act through `gh` or REST:** prefer `gh` for common operations; use REST/GraphQL for missing features.
4. **Verify:** read back issue/PR/repo state, CI status, and any comments or labels created.
5. **Report durable handles:** URLs, PR numbers, issue numbers, commit SHAs only in the task summary, not memory.

## Sub-workflows

### Authentication
Validate `gh auth status` before GitHub operations. Keep tokens out of logs and summaries.

### Repository management
For repo creation/forking/releases, inspect remotes and default branch first; verify the resulting URL and permissions.

### Issues
Use structured bodies, labels, assignees, and milestone fields. Search for duplicates before creating new issues.

### Pull requests
Keep branches focused, write useful PR bodies, monitor CI, and handle merge strategy explicitly.

### Code review
Review the diff, not just files. Separate blocking correctness/security issues from style suggestions; cite paths and lines.

### Codebase inspection
Use LOC/language inventory tools before estimating scope or reviewing unfamiliar repos.

## Common Pitfalls

1. Skipping `gh auth status` leads to half-finished operations.
2. Reporting success before reading back the GitHub object is unsafe.
3. Inline comments require exact diff positions; fall back to a review summary if positions are unavailable.
4. For third-party skill installs, check the host's current installation documentation and scan the complete runtime bundle before activation. A `dangerous` verdict blocks activation under the current host policy; do not bypass it with direct copies, trust reclassification or file omissions. Continue read-only diagnosis where useful: defensive examples may trigger heuristics, but isolated false positives do not prove bundle safety. Resolve findings through reviewed fixes and rescan rather than repeating an unchanged install. For a collection of independently installable skills, use the host policy decision on each complete bundle: even `caution` may block community skills. Verify admitted bundles through the host registry; keep a bulk setup that would activate blocked bundles separate.
5. Separate repository download, security admission and host compatibility. A fetched `SKILL.md` is not a working integration when the source also requires hooks, roles or executable services. Route full host ports through `software-delivery-workflows`; do not replace requested feature parity with a core-only skill.

## Verification Checklist

- [ ] Auth and repository target verified.
- [ ] Operation completed with a GitHub URL or status handle.
- [ ] CI/review/issue state was read back after mutation.
