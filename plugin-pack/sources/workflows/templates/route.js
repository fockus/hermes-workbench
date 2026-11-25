export const meta = {
  name: 'route',
  description: 'Classify the request and run the pattern that fits it, a single agent included',
  whenToUse: 'A workflow was asked for without naming a pattern. The classifier may pick a single agent, chain, parallel, orchestrate, or evaluate (offered only when max_iterations and threshold are given).',
}

// args: { task: string, max_iterations?, threshold?, criteria?, gate? }
const task = args?.task
if (typeof task !== 'string' || task.trim() === '') throw new Error('route needs args.task')
const guarded = Number.isInteger(args.max_iterations) && typeof args.threshold === 'number'
// The single-agent route comes first: when in doubt, one agent is the cheapest correct answer.
const routes = ['single-agent', 'chain', 'parallel', 'orchestrate', ...(guarded ? ['evaluate'] : [])]
const schema = {
  type: 'object',
  properties: {
    route: { type: 'string', enum: routes },
    steps: { type: 'array', items: { type: 'string' } },
    reason: { type: 'string' },
  },
  required: ['route'],
}
const choice = await agent(
  'Pick how to run this task. single-agent: one agent can do it (the default). chain: steps that each '
    + 'need the previous result; list them in steps. parallel: independent parts; list them in steps. '
    + 'orchestrate: the steps must be planned first. '
    + (guarded ? 'evaluate: the result is scored and refined until it is good enough. ' : '')
    + `\n\nTask: ${task}`,
  { label: 'route', schema })
const route = routes.includes(choice?.route) ? choice.route : 'single-agent'
const steps = Array.isArray(choice?.steps) ? choice.steps.filter((s) => typeof s === 'string' && s.trim()) : []
log(`route: ${route}${choice?.reason ? ` (${choice.reason})` : ''}`)

let output
if (route === 'single-agent') {
  output = await agent(task, { label: 'single-agent' })
} else if (route === 'chain') {
  output = await workflow('chain', { task, steps: steps.length ? steps : [task], ...(args.gate ? { gate: args.gate } : {}) })
} else if (route === 'parallel') {
  output = await workflow('parallel', steps.length > 1 ? { mode: 'sectioning', task, parts: steps } : { mode: 'voting', task })
} else if (route === 'orchestrate') {
  output = await workflow('orchestrate', { task })
} else {
  output = await workflow('evaluate', {
    task, max_iterations: args.max_iterations, threshold: args.threshold, ...(args.criteria ? { criteria: args.criteria } : {}),
  })
}
return { route, reason: choice?.reason ?? null, output }
