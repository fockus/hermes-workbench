---
name: software-delivery-workflows
description: "Use when delivering code or porting agent integrations."
version: 1.0.0
author: Hermes Workbench contributors
license: MIT
metadata:
  hermes:
    tags: [software-development, planning, debugging, testing, code-review, inspection, spike]
    related_skills: [github-operations]
---

# Software Delivery Workflows

## Overview

This umbrella covers the core engineering loop: plan, inspect, spike, debug, test-drive, implement, request review, and verify. Use language/debugger-specific tactics as subsections rather than standalone one-off skills.

## When to Use

- Write an actionable plan before execution.
- Inspect codebase size, languages, or structure.
- Run a throwaway spike to validate a technical approach.
- Debug Python, Node.js, or general runtime failures.
- Use test-driven development or repair test coverage.
- Prepare a pre-commit review with security and quality gates.

## Workflow

1. **Frame:** restate objective, constraints, repo path, and done criteria.
2. **Inspect:** read files, dependencies, tests, and execution entrypoints.
3. **Reproduce:** run the failing command or minimal example.
4. **Change:** make the smallest coherent patch.
5. **Verify:** run relevant tests/builds/linters and inspect artifacts.
6. **Review:** check diff for security, maintainability, and accidental changes.

## Sub-workflows

### Planning
Plans belong in `.hermes/plans/` when the user requests plan mode; do not execute in plan-only mode.

In plan-only mode return the plan without changing the repository. For approved implementation, run a failing check for the first bounded increment, make the change and verify it. Separate live deployment from isolated tests. Report actual files and test results in the requested language.

### Codebase inspection
Use language/LOC tools to size the codebase before estimating effort or review strategy.

When transferring reviewed code between a worker snapshot and the checkout, check byte-level hashes at the filesystem boundary. Rendered file reads can redact a harmless identifier such as a parameter named `authorization`; do not reconstruct source by stripping line numbers from a displayed read. Apply targeted diffs against verified base bytes, then compile and run through the actual host interpreter.

### Spikes
Keep spikes disposable and clearly separated from production edits.

### Systematic debugging
For a test that passes alone but fails in a suite, run the failing case alone, then replay the suspect predecessors and the case in order in **one interpreter** (`python -m unittest <first> <second> <case>`). Use a fresh process for each ordering; separate fixture directories do not isolate process-wide state. Turn the suspected mechanism into a deterministic test with a control. For cancellation, contrast a worker whose future already raised with one still blocked at its deadline: only the latter should receive a stop. After the fix, rerun the focused pair, the original order and the containing module. Report the mechanism you observed separately from an inferred cause such as thread-ID reuse.

For concurrent filesystem failures, do not accept a single green suite. Reproduce the syscall pattern in a bounded isolated probe, add a deterministic regression before the fix, and repeat fresh-process stress runs afterward. Distinguish a demonstrated call-pattern race from an unproven kernel defect; preserve fail-closed behavior for genuine disappearance and unsafe inode types rather than masking errors with broad retries.

### Python and Node debugging
Use pdb/debugpy or Node inspector/CDP when print/log inspection is insufficient.

For agent-host integration tests, locate and execute the runtime's own managed or isolated interpreter before installing dependencies. If an extension resolves from the host's site-packages instead of the runtime's, remove inherited Python overrides before retesting (for example, `env -u PYTHONPATH -u PYTHONHOME .venv/bin/python -m pytest` on POSIX). For a CLI script that imports sibling modules, use `python -E -s path/to/script.py` when you need to ignore ambient overrides without dropping the script directory from imports. Run the real CLI and the intended linter/type-checker; imports alone don't prove the runtime works. Keep fixtures under the configured scratch root and source overrides explicit so tests cannot mutate the live profile.

### Offline Electron smoke tests

Use the project's existing E2E harness from a separate dependency mirror; compile only the app entry points and renderer when full packaging could stage or download dependencies. Audit the backend resolver for a non-bootstrap branch before launching, and use an unpackaged bundle, an explicit source root, a managed interpreter, and a sanitized environment. Verify that the fully resolved scratch HOME, profile, and control-socket paths fit the platform's AF_UNIX budget before launch: TMPDIR alone does not prevent a socket from falling back to system `/tmp`. Confirm independent boundaries (owner readiness, Electron first window, authenticated wire, actual UI action) so a startup timeout does not masquerade as a product regression or a passing transport check. In a wire-only test, use the authoritative prompt receipt for terminal turn state unless the test deliberately attaches a viewer and exercises event delivery; a finished model stream alone does not prove a durable transcript. Capture native stderr from process launch before the window handle exists. Use a minimal Electron control under the same isolation to separate app-specific singleton/startup errors from Chromium helper sandbox failures; do not weaken sandbox settings or equate a generic lock message with a live competing instance.

### Test-driven development
Follow red-green-refactor and verify the test fails for the expected reason before implementation.

### Review request
Run quality/security checks, inspect the diff, and fix obvious issues before asking a human or remote reviewer.

### Cross-agent skill and memory-system ports
1. Establish the source feature contract before choosing packaging. Where full parity is required, this includes behavior parity, including hooks, subagent execution, GraphRAG, scripts, commands, CLI where needed, and the pipeline execution engines—not just memory storage. Preserve project-local pipeline steps/rules, work/team/drive/flow behavior, and configurable models/providers and reasoning effort at role, step and track levels. Copying instructions or installing only the core is not an acceptable substitute; implement missing host integration rather than silently removing features.
2. Discover the target host's live capabilities through its tools, current official documentation and installed source. In Hermes Desktop, query `desktop_project(action="list")` before assuming projects are absent; an empty list means no projects have been created, not that the feature is unsupported.
3. Preserve the user's memory boundaries: each project owns its bank and retrieval indexes; only chats the host positively identifies as outside all projects share the general RAG. Refuse capture and recall when the host cannot establish ownership; a missing association is not evidence of a general chat. Do not merge project content into general recall by default, infer project identity from the process working directory, or treat global skill installation as project initialization.
For opt-in native plugins exported as subdirectories of a fork, validate each entire plugin with `hermes plugins validate <path> --json` before installing by the documented method; a clean default-disabled registration is not a functional test. Verify both the profile enable list and each plugin's own settings gate. Do not invent real project, chat or SSH bindings.

4. Build a source-feature → host API → implementation gap → acceptance-test matrix. Separate download failures, security admission, dependency readiness and runtime compatibility; one failure does not establish the others.
5. Implement through contract-first tests and supported extension points. Keep activation blocked until security admission and lifecycle tests pass; do not relabel or repackage blocked content to evade a guard.
6. Verify actual hook dispatch, role execution, pipeline gates/resume, graph queries, semantic retrieval and scope isolation before claiming parity. Verify model/provider and reasoning effort at the actual outgoing execution boundary; YAML values, printed plans and fake launchers do not prove they were honored. Preserve configuration precedence and provenance, distinguish task-size tiers from reasoning effort, and reject unsupported settings rather than silently falling back. Prepared plans, installed files and enabled plugins are not evidence of working integration.

See [Agent-host integration](references/agent-host-integration.md) for discovery, scope and parity-test details.

## Common Pitfalls

1. Planning skills should not mutate code unless the user explicitly exits plan mode.
2. Debugging without a reproduction produces brittle fixes.
3. Passing one narrow test is not enough when the change affects broader behavior.

## Verification Checklist

- [ ] Objective and done criteria are explicit.
- [ ] Relevant tests/builds/linters were run or blockers reported.
- [ ] Diff was reviewed for unintended changes.
