export const meta = {
  name: 'evaluate',
  description: 'Generate, score, refine: rounds of a generator and a scorer until a quality threshold, a plateau or max_iterations',
  whenToUse: 'The quality of the result can be measured (a rubric, a test score) and refining it pays off. Needs args.max_iterations and args.threshold (0..1). Not when there is no measurable criterion or one good attempt is enough.',
}

// args: { task: string, max_iterations: integer >= 1, threshold: number in 0..1, criteria?: string }
// Stops at the threshold, at max_iterations, or after two rounds without beating the best score.
const maxIterations = args?.max_iterations
const threshold = args?.threshold
if (!Number.isInteger(maxIterations) || maxIterations < 1) {
  throw new Error('evaluate refuses to start: args.max_iterations (a positive integer) is required')
}
if (typeof threshold !== 'number' || !(threshold >= 0 && threshold <= 1)) {
  throw new Error('evaluate refuses to start: args.threshold (a number from 0 to 1) is required')
}
const task = args.task
if (typeof task !== 'string' || task.trim() === '') throw new Error('evaluate refuses to start: args.task is required')
const criteria = args.criteria ?? 'how well it does the task'

const scoreSchema = {
  type: 'object',
  properties: { score: { type: 'number' }, feedback: { type: 'string' } },
  required: ['score'],
}
let best = null
let stale = 0
const history = []
let stopped = 'max_iterations'
for (let round = 1; round <= maxIterations; round++) {
  const prompt = best === null ? task
    : `${task}\n\nImprove on this draft (score ${best.score}):\n${best.draft}\n\nReviewer feedback: ${best.feedback ?? 'none'}`
  const draft = await agent(prompt, { label: `generate:${round}` })
  let score = null
  let feedback = null
  if (typeof draft === 'string' && draft.trim() !== '') {
    const verdict = await agent(
      `Score this answer from 0 to 1 for ${criteria}, and say what to improve.\n\nTask: ${task}\n\nAnswer:\n${draft}`,
      { label: `score:${round}`, schema: scoreSchema })
    score = typeof verdict?.score === 'number' && verdict.score >= 0 && verdict.score <= 1 ? verdict.score : null
    feedback = verdict?.feedback ?? null
  }
  history.push(score)
  if (score !== null && (best === null || score > best.score)) {
    best = { draft, score, round, feedback }
    stale = 0
  } else {
    stale++
  }
  if (best !== null && best.score >= threshold) { stopped = 'threshold'; break }
  if (stale >= 2) { stopped = 'plateau'; break }
}
return { best, rounds: history.length, history, stopped }
