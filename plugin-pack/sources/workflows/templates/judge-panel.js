export const meta = {
  name: 'judge-panel',
  description: 'Write several candidates in parallel (sectioning), then a panel of judges votes for the best (voting)',
  whenToUse: 'Several distinct attempts are worth comparing and the best one is picked by judgement (names, designs, wording). Not when one attempt is enough, or when a score can rank them (use evaluate).',
}

// args: { task: string, candidates?: number (default 3), judges?: number (default 3) }
const task = args?.task
if (typeof task !== 'string' || task.trim() === '') throw new Error('judge-panel needs args.task')
const count = args.candidates ?? 3
if (!Number.isInteger(count) || count < 2 || count > 10) throw new Error('judge-panel args.candidates must be 2..10')

const made = await workflow('parallel', {
  mode: 'sectioning',
  task,
  merge: false,
  parts: Array.from({ length: count }, (_, i) => `candidate ${i + 1} of ${count}: make it different from the others`),
})
const candidates = made.sections.map((s, i) => ({ id: i + 1, output: s.output })).filter((c) => c.output !== null)
if (candidates.length === 0) throw new Error('judge-panel: no candidate was written')

const panel = await workflow('parallel', {
  mode: 'voting',
  n: args.judges ?? 3,
  choices: candidates.map((c) => String(c.id)),
  task: `Judge which candidate does this task best; answer with its number.\n\nTask: ${task}\n\n`
    + candidates.map((c) => `## Candidate ${c.id}\n${c.output}`).join('\n\n'),
})
if (panel.answer == null) throw new Error('judge-panel: the judges reached no majority')
const winner = Number(panel.answer)
return { winner, output: candidates.find((c) => c.id === winner).output, candidates, counts: panel.counts }
