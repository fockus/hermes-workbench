# Agent-host integration and memory-system parity

Use when transferring a skill-backed workflow between coding-agent hosts, especially when it includes persistent memory or executable integrations. This is a discovery and acceptance procedure, not a claim that any particular port is implemented.

## 1. Inventory the source before installing

- Inspect the repository's skill, runtime entrypoints, hook registrations, role definitions, command definitions, dependency declarations and tests.
- Count inventories programmatically from tracked source, separating dispatchable roles from composition partials and hook event names from callback registrations. A README's headline count is not an executable feature contract. Label static test declarations separately from collected, parametrized or passing tests.
- Generate a parity ledger with pending status and empty evidence for every runtime component. File coverage is only the starting point: enumerate command-router branches and configurable behavior before claiming functional coverage.
- Record a source revision for reproducibility in the task artifact, not in this reusable guide.
- Map every source feature to a target capability, an implementation gap and a verification case. Preserve all requested features; mark unsupported paths explicitly instead of substituting a smaller product.
- Distinguish portable business logic from host-specific event payloads, paths, permissions and command dispatch.

## 2. Discover target capabilities and deployment boundaries

For Hermes, start with the authoritative documentation at `https://hermes-agent.nousresearch.com/docs` and inspect the installed version's source when behavior depends on an API detail.

- Query `desktop_project(action="list")` to identify the selected Desktop project. For the file-backed `project-memory-bank` adapter, check `bank.bound(session_id)` separately: Desktop selection, the repository's `.memory-bank/`, and the plugin's session binding are different facts. For authorized work on an explicit project, resolve the expected existing bank with `bank.locate(project)`, stop on a conflicting binding, bind an unbound session with `bank.bind(session_id, project)`, and verify `bank.bound(session_id)` equals the expected bank before calling it active. Do not initialize a bank during a status audit. Distinguish the plugin's private handoff directory from the repository's canonical plan rather than writing parallel copies.
- Read plugin registration, memory-provider lifecycle, command/CLI dispatch and delegation contracts. Verify event timing and payloads rather than equating similarly named hooks.
- For Hermes fork comparisons, pin both commits (`git rev-parse HEAD`), find their common ancestor (`git merge-base`), and inventory the fork's changes from that ancestor. Then check the current local tree for equivalent behavior: a fork-only file path does not establish a missing feature.
- Check `hermes plugins list --user --plain` alongside `hermes memory status` in the target profile. Report separately whether the code is present, the plugin is enabled, the provider is selected, and recall has been exercised. A selected provider is not evidence of successful recall.
- Inspect only necessary non-secret configuration fields. Do not bulk-read credentials or import unrelated archives during capability discovery.
- When changing a global agent rule, locate the actual instruction sources and any loader-owned copies, patch the requested rule without editing unrelated templates, and distinguish changed files from protected or generated copies that were not changed. Check the effective prompt before claiming the rule is live; an open session can retain the old snapshot. On a Hermes build exposing `hermes sessions refresh-prompts`, use `hermes sessions refresh-prompts --session <confirmed-id>` to update only that conversation without losing history or restarting. A successful command schedules adoption on the next turn; verify the rebuilt prompt or next reply separately. Do not confuse a gateway restart with a prompt refresh.
- Prefer supported plugin APIs. If a host extension is required, specify the missing contract and its tests before changing host code. Probe session-project association from the plugin call surface, not the Desktop project's display or `session_cwd()`: if the stock host has no authoritative snapshot, return a scope error and test against the isolated host contract. Confirm the session and scope again before releasing slow retrieval results.
- Discover the active runtime interpreter from the installed launcher or documented runtime commands. Do not guess virtual-environment paths.
- For exclusive Hermes providers, check both the general manager's metadata-only listing and the selected memory factory. Run readiness against independently bound native dispatch identity captured before agent init republishes its session ID; do not fabricate that identity for status or validation. Exercise observer participation in an unrelated session and during manager replacement/unload, not just the selected session's happy path.

### Side-by-side deployment on a shared Colima host

1. Before starting an installer, inspect its root requirements. For private data or credentials, choose a new physical path and check its ancestors for symlinks and `.git`; an ignored directory can still be inside a worktree.
2. Record `docker context show`, inspect `colima list`, and check the intended VM's mounts with `colima ssh --profile "$profile" -- mount`. A directory that exists on macOS may be absent from the Docker daemon's filesystem.
3. If the required directory is not mounted and restarting the existing VM would interrupt other services, offer a separate VM and state the resource cost. On approval, create an owner-only parent outside Git; start a fresh profile with `colima start --profile "$profile" --activate=false --mount "$private_parent:w"`. Confirm the mount, inspect the new context with `docker --context "$context" info`, and check that the original context is still selected. Scope the installer with `DOCKER_CONTEXT="$context"` rather than switching the user's default.
4. If the installer needs device login, run it in a user-accessible interactive terminal. Direct the user to the provider's official page and that terminal for the code; do not copy credentials from a working profile. Report **awaiting sign-in** until authorization completes. After sign-in, test a synthetic fact through the intended agent and retrieve it from a fresh conversation; a downloaded model, healthy container or accepted write is not that test.

## 3. Define memory isolation before capture

| Context | Required boundary |
|---|---|
| Project with a folder | Bank belongs to that project root; reuse existing data without silent migration. |
| Project without a folder | Define a stable managed root keyed by trusted project identity. |
| Chat without a project | Use an explicit profile-local general-memory namespace, not the home directory as a pseudo-project. |
| Subagent or delayed task | Carry immutable parent scope into the task and its pending writes. |
| Chat moved between projects | Resolve future operations against the new association while preserving provenance of earlier work. |

When scope changes while a child is running, test what the host actually authorizes. Hold a harmless child operation on a barrier, move the parent to a second synthetic project and then release it. The new project must not acquire the old project’s records. A copied parent context is not independent child authority. For cancellation and shutdown, check separately whether work entered the handler, whether bytes became visible, and whether the caller received a durable acknowledgement.

Read project identity from trusted session metadata. A missing identity is not a general chat. Check the primary bank and each selected folder against one catalog snapshot. Repeat the test after changing the catalog, including ambiguous shared roots. Trace a real client operation to the actual command handler. Distinguish isolated handlers, transport tests and visible UI effects. For restart and replay, verify the receipt and persisted record count; a repeated request must not duplicate the original action.

Implement session association changes with a monotonic revision and compare-and-swap: an update must name its expected revision, so delayed callbacks cannot overwrite a newer project binding. Return frozen bindings for queued work; changing current routing must not mutate those bindings. Invalidate current routing before attempting to resolve an uncertain workspace move, because failed validation must not leave stale project routing silently usable. Reconstruct process-local bindings from authoritative host metadata after restart; an in-memory directory is not persisted host integration.

At filesystem boundaries, bind the canonical root to its filesystem identity and revalidate it on access. If authority lasts from attachment to a later operation, pin the bank and journal identities at attachment too. Compare them before opening records, and test replacement directories with identical owner bytes; a valid owner marker cannot make a replacement the originally attached namespace. On POSIX, descriptor-relative traversal with no-follow opens can reject symlink escapes and replaced roots; reject unsafe relative names, special files and shared hardlinked leaves before writing. Keep reads and scope resolution free of implicit directory creation. Test traversal, root replacement and outside-file preservation with real scratch fixtures. State the boundary honestly: a caller-supplied association is not authentication, and these checks are not an OS sandbox against a hostile process that can rename open directories or change mounts.

Store source/session provenance and idempotency information with captured records. Define how rename, move, deletion, retry and reassignment affect records and indexes. Treat historical-chat import as an explicit migration, not an automatic consequence of enabling capture.

Distinguish global personal facts from the general-chat RAG corpus. Audit other active capture/recall providers for duplicate ingestion and cross-project leakage before activating a new provider; do not disable them silently. Inspect the target version's provider-selection contract before reserving a slot: a single-provider host may require an explicit switch, while a fork with multi-provider support needs an explicit provider list and duplicate-capture checks. Treat document retrieval as a separate, explicitly scoped index and history views as read-only presentation, not additional owners of project facts. Verify capture and recall independently: a provider can ingest project facts from transcripts or tool results even when no project folder is indexed.

## 4. Translate behavior, not labels

- Session start: restore the correct bank and account for resume, not just new sessions.
- Before and after tool calls, inspect the actual accepted arguments and decision returned by the host. Check nested tool calls and errors; a logged warning is not proof that the action stopped. An after-event can close a matching earlier operation, not authorize a new one.
- Pre-compaction: bind the checkpoint to the exact ordered conversation and ownership snapshot. Require independently readable evidence before dispatching a summary or replacing history. Test a failed save, a concurrent edit, a retry and a revoked scope with disposable fixtures. Check only expected terminal bookkeeping changes; do not infer consent from an existing digest.
- On cancellation, distinguish acknowledgement from a worker stopping or a write rolling back. On restart, reconcile the durable intent before sending another mutation.
- Commands: verify public registration and actual dispatch separately. Check aliases and collisions using a disposable profile; an older host must refuse an unsupported non-replacing registration, not overwrite an existing command.

- Roles: preserve prompts, tool boundaries, model/effort requirements, budgets and parent verification. Do not silently replace named-role execution with instructions to the primary agent.
- Retrieval: test graph construction and structural queries separately from embeddings and semantic recall. Keyword fallback must not be reported as semantic parity. In an independent rerun, explicitly point at the pinned local weights and fail the run on a skipped dense test; pair a paraphrase with no lexical hit against an unrelated control. Record automatic reindex/revocation and chat recall as separate acceptance gates.

### Pipeline configuration and execution parity

1. Read the shipped pipeline schema/defaults, command contracts and resolver implementation together. For Memory Bank, inspect `references/pipeline.default.yaml`, `docs/pipeline-yaml.md`, `commands/work.md`, `commands/team.md` and the work/team resolver modules. Do not infer engine semantics from the command names.
2. Preserve project overrides, role composition, selected steps, verification cadence, dependencies, review/judge/fix loops, severity gates, QA, budgets, protected paths, resource locks and checkpoint/resume. Translate all supported user flags instead of hard-coding a default step order.
3. Test configuration precedence against upstream fixtures. Team settings resolve from size preset through team overrides, init attributes, track attributes and explicit prompt settings; retain where each effective value came from. Preserve independent executor, verifier, reviewer, judge, lead and review-lens settings where the source supports them.
4. Resolve exact model identifiers and premium/mid/inherit slots only through explicit host mappings. Do not repair tokens, substitute Claude aliases, reuse the parent model implicitly or confuse task complexity with reasoning effort. Validate provider capabilities before dispatch; an unavailable setting is an explicit incompatibility, not permission to ignore it.
5. Trace effective provider/model/effort through the resolver, role renderer, launch request and outgoing model transport. Test distinct settings on multiple steps/tracks. A mocked transport proves the local contract only; keep real execution-boundary validation as a separate acceptance gate.
6. Exercise execution, not just configuration: work must consume plan/spec items and actual test evidence; team tracks must run through the designated work engine; drive must schedule ready dependencies; flow must execute its selected route. Verify interruption/resume, bounded repair cycles, file ownership and cancellation without duplicate dispatch or false completion.
7. Separate durable workflow state from process-local child handles. Test recovery from stored run/track state after a restart rather than claiming an old subagent handle remains reconnectable. For an extracted distribution, compare the untouched native entry (no injected authority) with a separately registered test host and verify every product import came from the extraction. In fresh processes, contrast resuming a checkpointed stage with crashing after the file write but before acknowledgment: require zero repeat writes in both, and refusal on ambiguous inflight state. Fixture review/judge identities do not establish independent live roles.
8. Test named pipeline-file selection and workflow preset/flag composition as separate precedence ladders. Compare against the pinned upstream commands, not README shorthand. Test an explicitly unknown profile both at the selector and at its workflow entrypoint: a wrapper may discard a nonzero selection result and silently build the bundled workflow. Record actual stage lists, refusal reasons and unchanged config hashes after task overrides. Preserve existing file formats and behavior while designing additional per-stage routes.
9. Distinguish a fallback coding role from a fallback model route and from a code-repair loop. Require ordered and explicit executor/provider/model/effort candidates, allowed failure classes and scope/budget continuity. Do not rotate models on a deterministic test or judge failure, bypass permissions, or retry an ambiguous attempt with possible partial writes before reconciling its effects.
10. Verify requirements with executable red/green proof tied to the same command, expected RED failure, spec/config revision and candidate tree. Keep LLM review independent and unable to override a failing or missing required machine gate; keep the check definition outside the builder's write scope.

## 5. Diagnose failures without changing the contract

Before decoding a parent tool result as a child-dispatch receipt, check that it has the expected success shape. On mismatch, surface the sanitized tool error: a JSON parser exception can hide a cancellation before the child even launched. Report the passing child scenario and the failing module separately; do not infer a child lifecycle defect from a parent admission failure.

Treat transport/download, scanner admission, dependency setup and runtime dispatch as separate layers. After an unchanged installer failure, inspect that layer instead of repeatedly trying equivalent identifiers. Use read-only source inspection to diagnose; do not turn a failed installer path into a recommended installation method.

A scanner verdict blocks activation under the current host policy. Inspect flagged lines in context: defensive examples and parsers can trigger heuristics, but a few false positives do not establish bundle safety. Resolve findings through review and supported fixes, retain the admission gate and rescan the complete proposed runtime. Do not omit functional components merely to obtain a passing scan.

## 6. Acceptance evidence

Require all of the following before declaring a full port complete:

- Two project fixtures and one unassigned-chat fixture with distinct retrieval markers; no cross-scope results by default.
- Project switching, concurrent work and delayed-write tests demonstrating stable ownership.
- Real start, turn, tool, pre-compaction, stop/end and resume dispatch tests, including crash/retry and duplicate delivery.
- Real role execution and artifacts verified by the parent, including actual model/provider/effort enforcement for differently configured steps and tracks.
- Pipeline fixture parity plus executed work/team/drive/flow scenarios covering gates, budgets, resource ownership, failure, cancellation and resume; command registration alone is insufficient.
- Real graph queries and semantic retrieval with source attribution.
- Every feature-matrix row implemented and verified, with no unchecked row hidden behind a broad success claim.
- Install/health-check/update/rollback tests that preserve user data, other agents and other profiles. Verify wheel-only discovery before staging adapters from the installed wheel, then list actual manifests in a fresh process. If the native updater requires a Git checkout, report its refusal instead of converting a manually staged tree into a pretend managed install. A same-wheel reinstall and removal back to absence are not a two-version upgrade/downgrade. Seed an unrelated file inside the plugin directory as well as outside it; verify hashes after rollback and remove only unchanged, package-owned entries.
- Security admission and live runtime evidence after activation, not only unit tests or an enabled-plugin listing.

Report separately what is implemented, tested, installed and active. Keep remaining host gaps explicit.
