export const meta = {
  name: 'loop-until-dry',
  description: 'Sweep the task again and again (parallel sectioning over areas) until a round finds nothing new',
  whenToUse: 'Exhaustive discovery where one pass misses things (bug hunts, audit findings, edge cases). Not when one pass is enough or the list has a known end.',
}

// args: { task: string, areas?: string[], max_rounds?: number (default 5) }
// Each agent lists one finding per line ("none" when it has nothing); a round with no new line ends it.
const task = args?.task
if (typeof task !== 'string' || task.trim() === '') throw new Error('loop-until-dry needs args.task')
const maxRounds = args.max_rounds ?? 5
if (!Number.isInteger(maxRounds) || maxRounds < 1) throw new Error('loop-until-dry args.max_rounds must be a positive integer')
const areas = Array.isArray(args.areas) && args.areas.length ? args.areas : ['the whole task']

const findings = []
const known = new Set()
let rounds = 0
let dry = false
while (rounds < maxRounds) {
  rounds++
  const sweep = await workflow('parallel', {
    mode: 'sectioning',
    merge: false,
    parts: areas,
    task: `${task}\n\nList each new finding on its own line, or none.`
      + (findings.length ? `\nAlready found (do not repeat):\n${findings.join('\n')}` : ''),
  })
  // A failed area (null output) was not read, so the round cannot count as dry.
  const missed = sweep.sections.filter((s) => s.output === null).map((s) => s.part)
  let fresh = 0
  for (const section of sweep.sections) {
    for (const raw of String(section.output ?? '').split('\n')) {
      const line = raw.trim().replace(/^[-*]\s+/, '')
      if (!line || /^none\.?$/i.test(line) || known.has(line)) continue
      known.add(line)
      findings.push(line)
      fresh++
    }
  }
  if (missed.length) return { findings, rounds, dry: false, missed }
  if (fresh === 0) { dry = true; break }
}
return { findings, rounds, dry }
