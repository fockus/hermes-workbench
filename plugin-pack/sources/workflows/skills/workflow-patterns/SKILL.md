---
name: workflow-patterns
description: "Pick the right shape for a multi-agent workflow: a single agent first, then chain, parallel, evaluate, orchestrate, or a composition (adversarial-verify, judge-panel, loop-until-dry). Use when the user asks for a workflow, or before writing a workflow script."
---

# Workflow patterns

Each pattern is a saved workflow in `workflows/templates/`: run it by name, or copy it as the start
of a new script.
A request that names no pattern runs `route`: its classifier picks one of the patterns below, and
it always offers the single-agent route.

## Decision tree

Walk it top to bottom and stop at the first yes.

1. **Can a single agent do it in one go?** Then run one agent; no workflow. More agents cost more
   tokens and add hand-offs that can lose context. This is the default.
2. **Are there sequential dependencies, where each step needs the previous result?** → `chain`.
   Each step is followed by a deterministic gate (a command that must exit 0, and a non-empty
   output); the first failing gate stops the chain. Args: `steps`, optional `gate`.
3. **Are there independent subtasks?** → `parallel`.
   - sectioning: the parts do not need each other; they run at once and a merge agent joins them.
     Args: `mode: "sectioning"`, `parts`.
   - voting: the same question to `n` agents; the majority answer wins. Args: `mode: "voting"`, `n`,
     optional `choices`.
4. **Is there measurable refinement, where a score says how good a result is?** → `evaluate`.
   Generator, then scorer (0..1), round after round. It needs `max_iterations` and `threshold` and
   refuses to start without both. It stops at the threshold, at `max_iterations`, or after two
   rounds without beating the best, and returns the best.
5. **Must the steps be planned from the task first (dynamic planning)?** → `orchestrate`.
   A planner returns the steps, workers run them, a synthesis agent combines the results.

Compositions, built on the five through nested `workflow()`:
- `adversarial-verify`: a draft through `chain`, then skeptics vote through `parallel` whether it
  holds. For claims that are costly when wrong.
- `judge-panel`: candidates through `parallel` sectioning, then judges vote for the best.
- `loop-until-dry`: repeated `parallel` sweeps until a round finds nothing new (bounded by
  `max_rounds`).

## When not to use each pattern

- **single agent**: not when the work clearly exceeds one context, or needs independent checks.
- **chain**: not for independent parts (they wait on each other for nothing; use `parallel`), and
  not without a gate a step can fail.
- **parallel**: not when the parts depend on each other (use `chain`); voting is not for open-ended
  writing, where answers never match exactly.
- **evaluate**: not without a measurable criterion, a threshold and an iteration cap; not when one
  good attempt is enough.
- **orchestrate**: not when the steps are already known (use `chain` or `parallel`); the planner is
  one more agent and can over-split.
- **route**: not when the user named a pattern; run that one.
- **adversarial-verify**: not for taste questions, or when a test or gate can decide.
- **judge-panel**: not when a score can rank the candidates (use `evaluate`).
- **loop-until-dry**: not when one pass is enough or the list has a known end.
