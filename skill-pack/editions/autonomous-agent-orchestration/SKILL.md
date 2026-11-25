---
name: autonomous-agent-orchestration
description: "Use when delegating or monitoring agents. Verify execution."
version: 1.0.0
author: Hermes Workbench contributors
license: MIT
metadata:
  hermes:
    tags: [agents, delegation, claude-code, codex, opencode, monitoring, kanban]
    related_skills: [hermes-agent]
---

# Autonomous Agent Orchestration

## Overview

This umbrella covers spawning, supervising, and harvesting results from autonomous coding agents and agent-like workers. Use it for Claude Code, Codex, OpenCode, kanban-style orchestrator/worker roles, and monitoring already-running local agent sessions.

## When to Use

- Delegate implementation, code review, or repo exploration to a coding-agent CLI.
- Compare which agent backend fits a task.
- Inspect existing agent sessions without starting a new one.
- Coordinate multiple workers through a kanban/work-queue pattern.
- Recover useful output from transcripts/logs after an agent stalls.

## Backend Selection

- **Claude Code:** strong for large repo edits and PR-oriented implementation.
- **Codex:** useful for OpenAI-backed code tasks and patch generation.
- **Pi / OpenCode:** use the CLI with a verified route for the requested provider and model; don't infer model access from the CLI name.
- **Kanban orchestrator/worker:** check `hermes kanban --help` for native swarm support, then use it for the task graph and review handoffs. Keep Memory Bank in charge of project workflow and gates rather than creating a competing plan in the dispatcher.
- **Session monitoring:** use process, tmux, JSONL, and log inspection to understand existing sessions before interfering.

## Hermes-first projects

- Choose a runner the operator has authorized. Record the requested provider, model and effort. Verify them in the worker environment. Keep credentials in the selected provider’s supported configuration. If the route is unavailable, do not substitute a different executor.

- Keep project plan, decisions and verified checkpoints in the explicitly bound project's Memory Bank. Give workers a scoped task brief and references to the artifacts they need, not a global memory dump. A scheduler's state is only its run status; Hermes owns final acceptance and sync.
- Use a secondary runner only as an optional runner for a named, approved workflow. Do not run a second scheduler for the same work. Pin and test the actual installed version: downstream shell gates can skip acceptance correctly while the CLI exits zero and claims success after a required DAG node failed. Require explicit per-node status and independently verified acceptance artifacts for that run/revision; do not rely on exit code or banner alone. Test success, failure, cancel and resume before using the bridge on a real repo. Prefer a direct OpenCode adapter when upstream marks its provider deprecated.

## Operating Pattern

1. Define a self-contained task: repo path, branch rules, constraints, tests, and expected artifact. For parallel implementation, define shared interfaces first and assign disjoint implementation, test and evidence files; reserve shared package files, integration and project tracking for the parent.
2. Verify the chosen executor in a bounded scratch task before dispatching real work. A version check proves the CLI starts, not that it can run the requested model or honor reasoning effort, permissions, or cancellation. For an isolated Kanban test, pin the board DB, workspaces root, and Hermes home explicitly per invocation; shell exports can persist. Verify task IDs and dependency edges in the scratch DB, and verify those IDs do not exist in the live DB. A dispatch dry run validates selection only. Before a real dispatch, verify the worker's model route and profile boundary independently. Give each coding child a complete deliverable with observed RED/GREEN commands, a dedicated worktree for parallel edits, ownership limits, the integration API, and explicit limits on installs, activation, and host changes. A worktree isolates edits, not access to files, credentials, or the network.
3. Monitor through the backend's supported delivery mechanism. For background delegation with completion notifications, do independent work and end the turn to receive results rather than polling or waiting. If the parent adds a shared contract while children run, steer affected children with its exact API and ownership, not permission to overwrite siblings' work.
4. Inspect returned artifacts and rerun the combined test suite yourself before closing a stage. Reconcile stale child claims about repository state or tooling against the live workspace; child reports may describe an earlier state. Distinguish mocked transport contracts from real host execution. For a first live coding route, use one synthetic project, one explicitly allowed executor and no reserve; keep the requirement and independent checker outside the worker's writable directory. Record the expected RED signature, identical GREEN argv, unchanged checker hash and actual CLI session/exit; a successful CLI turn without the machine result cannot close the job.
5. When the user asks to finish an accepted list, execute the next authorized step in the same turn. For a faster audit, check the remaining independent items together, reuse valid evidence, and give one bounded operator command for the parent-only action instead of repeated preflight handoffs. If one step needs permission, leave it blocked without retrying or routing around the gate; continue independent work instead of returning the whole list to the user. When asked whether work is running, distinguish the helper preparing a fix from the worker executing the user's task. Lead with the last verified change, say which process is active (or that none is), and give at most one action the user needs to take. Keep commands needing interactive approval with the interactive owner; do not send the same blocked operation through a succession of background helpers. Record verified results and handles in the task record.

## Common Pitfalls

- Verify permissions in the worker's actual environment, with an allowed target and a forbidden neighbor. Obtain explicit consent before expanding access; keep supervision intact. Call the workflow working only after the intended worker claims the test task and produces its expected terminal result. For Hermes Kanban, use [the scoped worker preflight](references/scoped-worker-preflight.md).

1. Do not let subagents claim success without verifiable artifacts.
2. Do not spawn a new agent when the user asked to inspect an existing one.
3. Do not mix multiple agents in the same dirty worktree without explicit coordination.

## Verification Checklist

- [ ] Task prompt was self-contained.
- [ ] Agent backend and working directory were explicit.
- [ ] Output was independently verified by the parent agent.
