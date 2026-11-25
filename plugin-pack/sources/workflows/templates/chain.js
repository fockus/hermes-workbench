export const meta = {
  name: 'chain',
  description: 'Run steps one after another, each fed the previous output and checked by a deterministic gate',
  whenToUse: 'The task splits into steps where each needs the result of the one before (draft, then fix, then test). Not for independent parts (use parallel) or a single prompt (use one agent).',
}

// args: { steps: (string | {prompt, gate?})[], task?: string, gate?: string }
// A step passes its gate when its gate command (step.gate, else args.gate) exits 0 and its output
// is not empty; the first step that fails stops the chain.
const given = Array.isArray(args?.steps) && args.steps.length > 0 ? args.steps : (args?.task ? [args.task] : [])
if (given.length === 0) throw new Error('chain needs args.steps (a non-empty list) or args.task')
const steps = given.map((step) => (typeof step === 'string' ? { prompt: step } : step))

// Given steps (from a classifier) are instructions; the task they work on goes into every step prompt.
const context = Array.isArray(args?.steps) && args.steps.length && args?.task ? `Task: ${args.task}\n\n` : ''
const outputs = []
let previous = null
for (let i = 0; i < steps.length; i++) {
  const step = steps[i]
  if (!step || typeof step.prompt !== 'string' || step.prompt.trim() === '') {
    throw new Error(`chain step ${i + 1} has no prompt`)
  }
  const prompt = context + (previous === null ? step.prompt : `${step.prompt}\n\nOutput of the previous step:\n${previous}`)
  const gate = step.gate || args?.gate
  const output = await agent(prompt, { label: `step-${i + 1}`, ...(gate ? { gate } : {}) })
  if (typeof output !== 'string' || output.trim() === '') {
    log(`chain stopped: step ${i + 1} did not pass its gate`)
    return { completed: false, stoppedAt: i + 1, outputs, output: previous }
  }
  outputs.push(output)
  previous = output
}
return { completed: true, stoppedAt: null, outputs, output: previous }
