export const meta = {
  name: 'parallel',
  description: 'Sectioning (independent parts, then a merge) or voting (n agents answer, the majority wins)',
  whenToUse: 'Sectioning: the task has independent parts that do not need each other. Voting: one question where several independent answers make the result more reliable. Not for steps that depend on each other (use chain).',
}

// args: { mode?: 'sectioning' | 'voting', task: string,
//         parts?: string[], merge?: boolean            (sectioning; merge defaults to true)
//         n?: number, choices?: string[] }             (voting; n defaults to 3)
const task = args?.task
if (typeof task !== 'string' || task.trim() === '') throw new Error('parallel needs args.task')
const mode = args.mode ?? (Array.isArray(args.parts) ? 'sectioning' : 'voting')

if (mode === 'sectioning') {
  const parts = Array.isArray(args.parts) ? args.parts : []
  if (parts.length === 0) throw new Error('parallel sectioning needs args.parts (a non-empty list)')
  const outputs = await parallel(parts.map((part, i) => () =>
    agent(`${task}\n\nYour part (work on this part only): ${part}`, { label: `section:${i + 1}` })))
  // An empty or whitespace-only reply carries no work, so it counts as a failed part.
  const sections = parts.map((part, i) => ({ part,
    output: typeof outputs[i] === 'string' && outputs[i].trim() ? outputs[i] : null }))
  if (args.merge === false) return { mode, sections, merged: null }
  const failed = sections.filter((s) => s.output === null).map((s) => s.part)
  if (failed.length) throw new Error(`parallel: part(s) failed, not merged: ${failed.join(', ')}`)
  const merged = await agent(
    `${task}\n\nMerge these independent parts into one result:\n\n`
      + sections.map((s) => `## ${s.part}\n${s.output}`).join('\n\n'),
    { label: 'merge' })
  return { mode, sections, merged }
}

if (mode !== 'voting') throw new Error(`parallel mode must be sectioning or voting, not ${mode}`)
const n = args.n ?? 3
if (!Number.isInteger(n) || n < 1 || n > 25) throw new Error('parallel voting needs args.n between 1 and 25')
const choices = Array.isArray(args.choices) && args.choices.length > 0 ? args.choices.map(String) : null
const answer = choices ? { type: 'string', enum: choices } : { type: 'string' }
const schema = { type: 'object', properties: { answer }, required: ['answer'] }
const replies = await parallel(Array.from({ length: n }, (_, i) => () =>
  agent(`${task}\n\nAnswer on your own; give only the answer.`, { label: `vote:${i + 1}`, schema })))
const votes = replies.map((r) => (r && typeof r.answer === 'string' ? r.answer.trim() : null))
const counts = Object.create(null)
for (const vote of votes) if (vote) counts[vote] = (counts[vote] ?? 0) + 1
// The winner needs a strict majority of the n requested voters (a failed vote counts against);
// otherwise there is no answer.
let winner = null
for (const vote of votes) if (vote && (winner === null || counts[vote] > counts[winner])) winner = vote
if (winner !== null && counts[winner] * 2 <= n) winner = null
return { mode, answer: winner, votes, counts }
