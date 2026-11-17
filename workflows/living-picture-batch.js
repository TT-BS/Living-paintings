export const meta = {
  name: 'living-picture-batch',
  description: 'Bring several pictures to life in parallel with the living-picture skill: make, independent review, fix',
  whenToUse: 'Several still pictures should each become a living picture page. args: images (absolute paths), root (absolute job folder), optional notes, skill, concurrency',
  phases: [
    { title: 'Make', detail: 'per picture: one agent follows SKILL.md end to end in root/<stem>/' },
    { title: 'Review', detail: 'per picture: a fresh agent renders its own frames and tests the claims against references/checking.md' },
    { title: 'Fix', detail: 'only where blocking or major issues were found or the review said not deliverable: fix causes, re-check' },
  ],
}

/* living-picture batch: one job folder per picture, three stages per picture, pictures in parallel.
 *
 * How to run it (Claude Code):
 *   - Workflow tool: scriptPath = "<skill folder>/workflows/living-picture-batch.js" (absolute), and
 *     args = { "images": ["/abs/a.jpg", "/abs/b.png"], "root": "/abs/jobs", "notes": "...", "skill": "/abs/skill" }
 *   - or copy this file into a project's .claude/workflows/ and run it by name: living-picture-batch
 * args
 *   images       absolute paths of the pictures
 *   root         absolute folder for the jobs (root/<stem>/): outside every git repository and published folder.
 *                Names that one Mac folder would share (Hero, hero) get -2, -3; a maker that finds another
 *                picture's job in its folder (from an earlier run) stops and reports instead of mixing them.
 *   notes        optional: what the person said for all of them (feeling, target page, display size); passed verbatim
 *   skill        optional: the skill folder (default ~/.claude/skills/living-picture)
 *   concurrency  optional: agents working at once (default 4, at most 10); each runs a browser and the cut tools
 * Stages, as a pipeline (a picture moves on as soon as its own previous stage is done):
 *   Make    an agent reads SKILL.md, follows it end to end and returns a summary of checkable claims
 *   Review  a fresh agent that never saw the maker's reasoning tests those claims on its own renders
 *   Fix     only for blocking or major issues, or a "not deliverable" verdict even when no such issue is listed:
 *           fix the causes, re-check, report the final state
 * The fixer checks its own work; there is no second independent review (the log says so).
 */

const A = typeof args === 'string' ? JSON.parse(args) : (args || {})
const images = Array.isArray(A.images) ? A.images : []
if (!images.length) throw new Error('args.images: give a non-empty array of absolute picture paths')
for (const p of images) {
  if (typeof p !== 'string' || !p.startsWith('/')) throw new Error('args.images: not an absolute path: ' + JSON.stringify(p))
}
if (typeof A.root !== 'string' || !A.root.startsWith('/')) throw new Error('args.root: give the absolute folder for the jobs')
const ROOT = A.root.replace(/\/+$/, '')
const NOTES = typeof A.notes === 'string' && A.notes.trim() ? A.notes.trim() : '(none)'
const LIMIT = Math.max(1, Math.min(10, Math.floor(Number(A.concurrency) || 4)))

// Shell-ready values: agents paste these lines, so paths with spaces or quotes stay intact.
const sq = (s) => "'" + String(s).replace(/'/g, "'\\''") + "'"
const SKILL_SH = typeof A.skill === 'string' && A.skill.trim()
  ? (A.skill.startsWith('~/') ? '"$HOME"/' + sq(A.skill.slice(2).replace(/\/+$/, '')) : sq(A.skill.replace(/\/+$/, '')))
  : '"$HOME/.claude/skills/living-picture"'

// One job folder per picture. Names are compared the way the default Mac volume (case-insensitive APFS) compares them:
// Hero and hero, or a composed and a decomposed é, are one folder there, so they count as taken and get -2, -3 ...
// Combining marks are kept (NFC first), and the name is never '.', '..' or hidden: leading dots go, nothing left
// becomes 'picture'.
const sameFolder = (s) => s.normalize('NFC').toUpperCase().toLowerCase()
const taken = new Set()
const jobs = images.map((image, index) => {
  const clean = (s) => s.replace(/[^\p{L}\p{M}\p{N}._-]+/gu, '-').replace(/^[-.]+|[-.]+$/g, '')
  const base = clean(Array.from(clean((image.split('/').pop() || '').normalize('NFC').replace(/\.[^.]+$/, '')))
    .slice(0, 60).join('')) || 'picture'
  let stem = base
  for (let n = 2; taken.has(sameFolder(stem)); n++) stem = base + '-' + n
  taken.add(sameFolder(stem))
  return { image, stem, dir: ROOT + '/' + stem, index }
})

// At most LIMIT agents at once, across all stages.
let active = 0
const queue = []
function limited(fn) {
  return new Promise((resolve, reject) => {
    const start = () => {
      active++
      Promise.resolve().then(fn).then(resolve, reject).finally(() => {
        active--
        const next = queue.shift()
        if (next) next()
      })
    }
    if (active < LIMIT) start(); else queue.push(start)
  })
}
const run = (prompt, opts) => limited(() => agent(prompt, opts))

const PRIVACY = `Privacy (binding for this run):
- The picture and everything made from it belong to the person. Never upload them or send them to any website, service or other model; looking at them yourself is the only exception.
- No package installs, no model downloads, no network fetches for this work. If a tool is missing, write it under open issues instead.
- Never run scripts/codex_bridge.py with --consent-upload, the Codex imagegen route, or anything else that sends pictures off this machine.
- Write only inside your job folder. Never modify the original picture or anything outside the job folder; do not copy the pictures elsewhere, publish or commit them.`

const setup = (job) => `Shell setup (paste at the start of each Bash command that needs it; keep the quotes):
  SKILL=${SKILL_SH}; LP="$SKILL/bin/living-picture"; IMG=${sq(job.image)}; JOB=${sq(job.dir)}`

const CHECK = {
  type: 'object',
  properties: {
    check: { type: 'string', description: 'what was checked (e.g. a row of the SKILL.md table or references/checking.md)' },
    result: { type: 'string', description: 'what the evidence shows, plainly; say "not verified" when it does not' },
    evidence: { type: 'array', items: { type: 'string' }, description: 'absolute paths of frames, sheets, crops or reports' },
  },
  required: ['check', 'result', 'evidence'],
}

const SUMMARY = {
  type: 'object',
  properties: {
    job: { type: 'string', description: 'absolute job folder' },
    delivered: { type: 'boolean', description: 'true only if out/index.html exists and plays' },
    page: { type: 'string', description: 'absolute path of out/index.html, or empty' },
    intent: { type: 'string', description: 'the picture in one sentence (SKILL.md rule 1)' },
    leading_dynamic: { type: 'string', description: 'the leading dynamic and why it carries the intent; target display size' },
    moves: {
      type: 'array',
      items: {
        type: 'object',
        properties: { what: { type: 'string' }, how: { type: 'string' }, why: { type: 'string' } },
        required: ['what', 'how', 'why'],
      },
    },
    stays: {
      type: 'array',
      items: { type: 'object', properties: { what: { type: 'string' }, why: { type: 'string' } }, required: ['what', 'why'] },
    },
    checks: { type: 'array', items: CHECK },
    open_issues: { type: 'array', items: { type: 'string' }, description: 'what is not done or not verified, plainly' },
  },
  required: ['job', 'delivered', 'page', 'intent', 'leading_dynamic', 'moves', 'stays', 'checks', 'open_issues'],
}

const REVIEW = {
  type: 'object',
  properties: {
    own_reading: { type: 'string', description: 'your own reading of the picture, written before reading the claims' },
    verdict: {
      type: 'string',
      enum: ['pass', 'issues', 'not deliverable'],
      description: '"not deliverable" when there is a blocking issue (list each under issues as blocking); "issues" for major or minor ones; "pass" only with evidence',
    },
    issues: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: ['blocking', 'major', 'minor'] },
          what: { type: 'string', description: 'what is wrong, where (layer, region, time)' },
          claim: { type: 'string', description: 'the claim or checklist row it contradicts' },
          evidence: { type: 'string', description: 'absolute path of the frame, sheet or crop that shows it' },
          cause: { type: 'string', description: 'likely cause, per references/checking.md section 3' },
        },
        required: ['severity', 'what', 'claim', 'evidence', 'cause'],
      },
    },
    confirmed: { type: 'array', items: { type: 'string' }, description: 'claims your own renders support' },
    not_checked: { type: 'array', items: { type: 'string' } },
    renders: { type: 'string', description: 'folder with your own renders' },
  },
  required: ['own_reading', 'verdict', 'issues', 'confirmed', 'not_checked', 'renders'],
}

const FIX = {
  type: 'object',
  properties: {
    fixed: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          issue: { type: 'string' },
          cause: { type: 'string' },
          change: { type: 'string', description: 'what was changed (plan, scene, code)' },
          evidence: { type: 'string', description: 'absolute path showing the issue gone at the same place and time' },
        },
        required: ['issue', 'cause', 'change', 'evidence'],
      },
    },
    not_fixed: {
      type: 'array',
      items: { type: 'object', properties: { issue: { type: 'string' }, why: { type: 'string' } }, required: ['issue', 'why'] },
    },
    delivered: { type: 'boolean' },
    page: { type: 'string' },
    checks: { type: 'array', items: CHECK },
    open_issues: { type: 'array', items: { type: 'string' } },
  },
  required: ['fixed', 'not_fixed', 'delivered', 'page', 'checks', 'open_issues'],
}

const makePrompt = (job) => `You are bringing one picture to life with the living-picture skill, end to end, on your own. Other agents work on other pictures in their own folders at the same time; leave those alone.

${setup(job)}

1. Read "$SKILL/SKILL.md" first and follow it: it is the authority on how to work, and its rules are the floor, not the ceiling. Read the references it points to when you reach those steps.
2. Create the job folder: "$LP" new "$IMG" --job "$JOB" (working copy, grid view, notes.md skeleton with the picture's path and sha256). If "$LP" is missing, use python3 "$SKILL/scripts/picture.py" prepare and look as SKILL.md shows, and record the picture's path and sha256 (shasum -a 256 "$IMG") in notes.md yourself.
   - If it refuses because "$JOB" is inside a git repository: stop and report that; pass --force only if the person's notes below allow it.
   - If it refuses because "$JOB" already holds a job: that job is yours only if its notes.md sha256 line equals the hash from shasum -a 256 "$IMG". Then continue it: read notes.md first and redo only what is affected. If it is another picture's job, or its picture cannot be confirmed, stop and report it, and never use --force or --reprepare on it: two pictures in one folder corrupt both.
   - If it says an earlier run did not finish there and that placeholder notes.md names "$IMG", delete that notes.md and run new again.
   - If its JSON says "watermark_removed": true, it found and removed a share-platform watermark: look at "$JOB/clean-review.jpg" at display size and 4x before going on. If the repair shows, redo it into the same file: "$LP" dewatermark "$IMG" --out "$JOB/clean.png" with rough --pill/--line boxes or --mode fill/untint, look at the new "$JOB/clean-review.jpg", then "$LP" new "$IMG" --job "$JOB" --reprepare, which prepares work/picture.png from "$JOB/clean.png" and never searches again. If the "mark" is part of the painting (a signature, say), "$LP" new "$IMG" --job "$JOB" --reprepare --keep-watermark prepares from the picture as received. Record what you did in notes.md.
   - If its JSON says the watermark search was "skipped" (a large picture) or "failed", and look.jpg shows a share-platform mark in the lower right, take the same dewatermark and --reprepare steps by hand.
   - If its JSON says "photo_corrected": true, the picture was a photo of the artwork and rectify cropped and straightened it (and corrected light and colour only where it found a neutral reference): look at "$JOB/rectified-review.jpg" at display size before going on (the corners on the artwork's real edges, straight sides, colours still this painting's). If the boundary is wrong, run the redo command new printed (rectify --corners with the four corners read off a grid view), then "$LP" new "$IMG" --job "$JOB" --reprepare; if the correction made things worse, --reprepare --as-is. If rectify found nothing but look.jpg shows a wall, a frame or a table around the artwork (often a work cut off by the photo's edge), do the same by hand with --corners. Pass rectify's warnings (glare, blur, steep angle, cut off, too few pixels) on in your open issues instead of hiding them. Record what you did in notes.md.
3. Work through SKILL.md 怎么做 1-6 in "$JOB": read the picture, write the dynamic script, plan and cut (iterate on work/review/), write the scene, bundle into "$JOB/out", capture and look, fix causes. Keep notes.md current after every milestone, not only at the end: a run cut off by a network outage then loses nothing, and the next run continues from it. "$LP" --help lists every tool; "$LP" vision exists only where the macOS Vision helper is installed: its output is a starting point you verify by looking, never the decision. "$LP" doctor tells what this machine has.
4. Deliver "$JOB/out/index.html" (with out/site/). Do not put it into any project; that is the person's step.
5. Nobody can answer questions during this run. Where SKILL.md would ask the person about the overall feeling, write your assumption in notes.md and continue.

Notes from the person, for every picture: ${NOTES}

${PRIVACY}

Return the summary. Every claim must be checkable: each check names the evidence files (absolute paths in the job folder) that show it. Anything not done or not verified (for example rhythm seen only in a frame strip) goes under open_issues; do not round up. An independent reviewer will test these claims on fresh renders.`

const reviewPrompt = (job, summary) => `You are an independent reviewer of a living picture made by another agent. You did not make it, you have not seen how it was made, and you owe it nothing: your job is to find what is wrong. A pass needs evidence.

${setup(job)}

1. Read "$SKILL/SKILL.md" (the rules and the table 交付前逐项看) and "$SKILL/references/checking.md".
2. Before you read notes.md or the claims below, look at "$JOB/work/picture.png" yourself ("$LP" look, with --box to zoom) and write one sentence on what the picture is about, what should lead, and what must stay still and why. That is your own_reading.
3. Make your own evidence in "$JOB/review/" only: never write into check/, and never edit plan.json, scene.json, work/ or out/. Give every tool an --out inside "$JOB/review/" (look and inspect otherwise write next to their input, inside work/). For example: "$LP" capture "$JOB/out/index.html" --out "$JOB/review/run" --every 0.5 --duration 12; the same with --pointer corners:1-9; --reduced-motion --probe 3; dense samples with --channels and --track to find peaks, then sharp frames at those times; "$LP" sheet with --motion, and with --box and --zoom on every moving edge and face; "$LP" inspect "$JOB/work" --layer <id> --pieces for released pieces; "$LP" diff for the original fallback; a short --video if you can judge continuous play.
4. Test each claim below against your renders and the checklist, and look for what the claims leave out.
5. Severity: blocking = breaks a bottom line of the SKILL.md table (fallback is not the lossless original; holes, old outlines or travelling ground at the extremes; sliding on a support; marks not from the painting; text or frame moving; the page does not play); major = a clearly visible defect at the target size, a necessary response missing, or the leading dynamic unreadable; minor = small or arguable. Each issue names the evidence path and the likely cause (references/checking.md section 3).
6. Verdict: "not deliverable" if anything blocking was found, and then each blocking finding is also an issue with severity blocking (the fix stage works from the issues); "issues" if only major or minor ones; "pass" only when the evidence covers what stills and video can show.

The maker's summary (claims to test, not facts):
${JSON.stringify(summary, null, 1)}

${PRIVACY}

Return the review.`

const serious = (review) => (review && Array.isArray(review.issues) ? review.issues : [])
  .filter((i) => i.severity === 'blocking' || i.severity === 'major')
// The verdict counts on its own: a page the reviewer calls not deliverable goes to the fix stage even when its issue
// list names nothing blocking or major (the schema cannot tie the two together).
const needsFix = (review) => !!review && (serious(review).length > 0 || review.verdict === 'not deliverable')

const fixPrompt = (job, review) => {
  const heavy = serious(review)
  const minor = (Array.isArray(review.issues) ? review.issues : []).filter((i) => i.severity === 'minor')
  const what = heavy.length
    ? `Issues to fix (blocking and major):
${JSON.stringify(heavy, null, 1)}`
    : `The reviewer's verdict is "not deliverable", but it listed no blocking or major issue. Find out why before anything else: start from what it could not check and its own reading below, confirm that "$JOB/out/index.html" plays at all (render a few frames with "$LP" capture into "$JOB/check/"), and treat whatever makes it not deliverable as a blocking issue. If you find nothing, say so under not_fixed instead of guessing.
What the reviewer could not check:
${JSON.stringify(review.not_checked || [], null, 1)}
Its own reading of the picture: ${JSON.stringify(review.own_reading || '')}`
  return `You are fixing a living picture after an independent review found it not ready (verdict: ${JSON.stringify(review.verdict)}). Fix causes, not symptoms.

${setup(job)}

Read "$SKILL/SKILL.md", "$JOB/notes.md" and the references the issues touch. For each issue: reproduce it from its evidence; find the cause (SKILL.md 5: a bad edge is a cut or fill problem, not an amplitude problem); change plan.json, scene.json or your own code; re-cut only what is affected; re-bundle into "$JOB/out"; re-capture into "$JOB/check/"; confirm the issue is gone at the same place and time and at the joint extremes, and that nothing nearby broke. If a cause cannot be fixed, switching that action off beats shipping it wrong: say so. Never shrink the motion to hide an error. Update notes.md (资产处理, 验证范围, 未决).

${what}

Minor issues (fix if cheap, otherwise list them as open):
${JSON.stringify(minor, null, 1)}

${PRIVACY}

Return the final state.`
}

log(`${jobs.length} picture(s) -> ${ROOT}/<stem>/, up to ${LIMIT} agents at once`)

const results = await pipeline(jobs,
  (job) => run(makePrompt(job), { label: 'make ' + job.stem, phase: 'Make', schema: SUMMARY }),
  async (summary, job) => {
    if (!summary) return { summary: null, review: null }
    if (!summary.delivered) return { summary, review: null }  // nothing plays: nothing to review
    const review = await run(reviewPrompt(job, summary), { label: 'review ' + job.stem, phase: 'Review', schema: REVIEW })
    const n = serious(review).length
    if (review && review.verdict === 'not deliverable' && !n) {
      log(`review ${job.stem}: verdict "not deliverable" but no blocking or major issue listed; it goes to the fix stage anyway`)
    } else if (review && review.verdict === 'pass' && n) {
      log(`review ${job.stem}: verdict "pass" but ${n} blocking or major issue(s) listed; they go to the fix stage`)
    }
    return { summary, review }
  },
  async (s, job) => {
    if (!needsFix(s.review)) return { ...s, fix: null }
    const fix = await run(fixPrompt(job, s.review), { label: 'fix ' + job.stem, phase: 'Fix', schema: FIX })
    return { ...s, fix }
  },
)

const report = jobs.map((job, i) => {
  const r = results[i]
  const base = { image: job.image, job: job.dir }
  // pipeline() drops an item whose stage threw to null; keep the same shape so the report below never breaks
  if (!r) {
    return { ...base, status: 'failed', note: 'a stage failed; see the run log', page: '', intent: '', leading_dynamic: '',
      review_verdict: null, issues: [], fixed: [], not_fixed: [], open_issues: [], not_checked: [] }
  }
  const { summary, review, fix } = r
  const undeliverable = !!review && review.verdict === 'not deliverable'
  let status
  if (!summary) status = 'not made (agent stopped)'
  else if (!summary.delivered) status = 'not delivered'
  else if (!review) status = 'delivered, review missing'
  else if (!needsFix(review)) status = review.verdict === 'pass' ? 'passed review' : 'minor issues only'
  else if (!fix) status = undeliverable ? 'not deliverable (review), fix agent stopped' : 'issues found, fix agent stopped'
  else if (fix.delivered && !fix.not_fixed.length) status = 'fixed (self-checked)'
  else status = undeliverable ? 'not deliverable (review), issues remain' : 'issues remain'
  return {
    ...base,
    status,
    page: (fix && fix.page) || (summary && summary.page) || '',
    intent: summary ? summary.intent : '',
    leading_dynamic: summary ? summary.leading_dynamic : '',
    review_verdict: review ? review.verdict : null,
    issues: review ? review.issues : [],
    fixed: fix ? fix.fixed : [],
    not_fixed: fix ? fix.not_fixed : [],
    open_issues: (fix ? fix.open_issues : summary ? summary.open_issues : []) || [],
    not_checked: review ? review.not_checked : [],
  }
})
for (const r of report) log(`${r.status}: ${r.job}`)
if (report.some((r) => r.fixed.length || r.not_fixed.length)) log('Fixes were re-checked by the fixer only, not by a second independent review.')
return report
