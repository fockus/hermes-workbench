export const meta = {
  name: 'orchestrate',
  description: 'A planner splits the task into steps, workers run each step, a synthesis agent combines them',
  whenToUse: 'The subtasks are not known in advance and must be planned from the task itself (a change across unknown files, a research question). Not when the steps are already known (use chain or parallel).',
}

// args: { task: string, max_steps?: number (default 8) }
const task = args?.task
if (typeof task !== 'string' || task.trim() === '') throw new Error('orchestrate needs args.task')
const maxSteps = args.max_steps ?? 8
if (!Number.isInteger(maxSteps) || maxSteps < 1) throw new Error('orchestrate args.max_steps must be a positive integer')

const step = { type: 'object', properties: { title: { type: 'string' }, prompt: { type: 'string' } }, required: ['title', 'prompt'] }
const plan = await agent(
  `Plan this task as at most ${maxSteps} independent steps a worker can do alone, each with a short title `
    + `and a self-contained prompt.\n\nTask: ${task}`,
  { label: 'plan', schema: { type: 'object', properties: { steps: { type: 'array', items: step } }, required: ['steps'] } })
const steps = plan?.steps ?? []
if (steps.length > maxSteps) throw new Error(`orchestrate: the planner returned ${steps.length} steps, over max_steps ${maxSteps}`)
if (steps.length === 0) throw new Error('the planner returned no steps')

const results = await pipeline(steps, (s) => agent(`${s.prompt}\n\nThis is one step of: ${task}`, { label: `step:${s.title}` }))
const done = steps.map((s, i) => ({ title: s.title, output: results[i] }))
const synthesis = await agent(
  `Combine the results of these steps into the answer to the task.\n\nTask: ${task}\n\n`
    + done.map((d) => `## ${d.title}\n${d.output ?? '(this step failed)'}`).join('\n\n'),
  { label: 'synthesis' })
return { plan: steps, results: done, synthesis }
