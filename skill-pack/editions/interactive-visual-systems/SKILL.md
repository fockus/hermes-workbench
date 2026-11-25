---
name: interactive-visual-systems
description: "Use when building or controlling node/workflow-based real-time visual systems such as ComfyUI image/video/audio workflows and TouchDesigner/twozero MCP operator graphs."
version: 1.0.0
author: Hermes Workbench contributors
license: MIT
metadata:
  hermes:
    tags: [comfyui, touchdesigner, workflows, realtime, node-graphs, generative-media]
    related_skills: [creative-visual-artifacts, audio-video-media-generation]
---

# Interactive Visual Systems

## Overview

This umbrella covers node/workflow engines where the task is not just a static artifact but a running graph: ComfyUI workflows and TouchDesigner/twozero MCP projects.

## When to Use

- Install, launch, or operate ComfyUI; manage models/nodes; execute workflows via REST/WebSocket.
- Control TouchDesigner through MCP tools: create operators, set parameters, wire connections, execute Python, and build realtime visuals.
- Debug node graph execution, missing dependencies, model paths, ports, or workflow schemas.

## Workflow

1. Check environment, hardware, dependencies, ports, and required assets/models.
2. Load or construct the workflow/operator graph.
3. Execute incrementally and inspect logs/errors.
4. Verify visible output, generated media, or node state.
5. Save/export the workflow or final artifact path.

## Sub-workflows

### ComfyUI
Use official lifecycle tools where available; validate workflow JSON and node schemas before execution.

### TouchDesigner
Use MCP operator tools to build and inspect graphs; prefer small verified graph changes over blind large scripts.

## Common Pitfalls

1. Missing model/node dependencies often masquerade as workflow bugs.
2. Long-running UI servers need readiness checks, not blind sleeps.
3. Node graphs require visual/output verification after execution.

## Verification Checklist

- [ ] Server/app was running and reachable.
- [ ] Required assets/models/nodes were present.
- [ ] Output media, rendered view, or node graph state was verified.
