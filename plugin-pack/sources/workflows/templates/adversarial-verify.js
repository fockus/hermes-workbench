export const meta = {
  name: 'adversarial-verify',
  description: 'Draft an answer through chain, then let independent skeptics try to refute it and take the majority verdict (parallel voting)',
  whenToUse: 'A claim or fix must be right and a wrong one is costly (a bug diagnosis, a security finding). Not for taste questions or when a deterministic check (a test, a gate) can decide.',
}

// args: { task: string, claim?: string (skip the draft), n?: number of skeptics (default 3), gate?: string }
const task = args?.task
if (typeof task !== 'string' || task.trim() === '') throw new Error('adversarial-verify needs args.task')
let draft = typeof args.claim === 'string' && args.claim.trim() ? args.claim : null
if (draft === null) {
  const made = await workflow('chain', { steps: [{ prompt: task, ...(args.gate ? { gate: args.gate } : {}) }] })
  draft = made?.completed ? made.output : null
}
if (draft === null) throw new Error('adversarial-verify: the draft did not pass its gate')

const vote = await workflow('parallel', {
  mode: 'voting',
  n: args.n ?? 3,
  choices: ['holds', 'refuted'],
  task: 'Act as a skeptic. Try hard to refute the answer below: look for a counterexample, a wrong '
    + 'assumption or a missed case. Answer refuted if you found a real flaw, holds otherwise.'
    + `\n\nTask: ${task}\n\nAnswer under review:\n${draft}`,
})
if (vote.answer == null) throw new Error('adversarial-verify: the skeptics reached no majority')
return { draft, verdict: vote.answer, counts: vote.counts }
