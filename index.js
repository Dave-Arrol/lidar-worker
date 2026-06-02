const express = require('express')
const { createClient } = require('@supabase/supabase-js')
const { execFile } = require('child_process')
const fs = require('fs')
const fsp = require('fs/promises')
const path = require('path')
const os = require('os')

// Clean env values: strip whitespace/newlines and any surrounding quotes
function clean(v) {
  if (!v) return ''
  return v.trim().replace(/^['\"]|['\"]$/g, '').trim()
}

const SUPABASE_URL = clean(process.env.SUPABASE_URL)
const SUPABASE_SERVICE_ROLE_KEY = clean(process.env.SUPABASE_SERVICE_ROLE_KEY)
const WORKER_SECRET = clean(process.env.WORKER_SECRET)
const RAW_BUCKET = clean(process.env.RAW_BUCKET) || 'lidar-raw'
const OCTREE_BUCKET = clean(process.env.OCTREE_BUCKET) || 'lidar-octree'
const PORT = clean(process.env.PORT) || 8080

// Helpful diagnostics on boot (does NOT print secrets)
console.log('ENV check -> SUPABASE_URL:', SUPABASE_URL ? `set (${SUPABASE_URL.length} chars, starts ${SUPABASE_URL.slice(0,8)})` : 'MISSING')
console.log('ENV check -> SERVICE_ROLE_KEY:', SUPABASE_SERVICE_ROLE_KEY ? `set (${SUPABASE_SERVICE_ROLE_KEY.length} chars)` : 'MISSING')
console.log('ENV check -> WORKER_SECRET:', WORKER_SECRET ? 'set' : 'MISSING')

if (!SUPABASE_URL || !SUPABASE_SERVICE_ROLE_KEY) {
  console.error('FATAL: Supabase env vars not found. Check Railway Variables names match exactly: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY')
}

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
const app = express()
app.use(express.json())

app.get('/health', (_req, res) => res.json({ ok: true }))

// Fire-and-forget: respond 202, then process in the background.
app.post('/process', (req, res) => {
  if (req.headers['x-worker-secret'] !== WORKER_SECRET) return res.status(401).json({ error: 'unauthorized' })
  const { jobId, rawPath } = req.body || {}
  if (!jobId || !rawPath) return res.status(400).json({ error: 'jobId and rawPath required' })
  res.status(202).json({ accepted: true })
  runJob(jobId, rawPath).catch(async (e) => {
    console.error('job failed', jobId, e)
    await supabase.from('lidar_jobs').update({ status: 'failed', error: String(e).slice(0, 500), updated_at: new Date().toISOString() }).eq('id', jobId)
  })
})

function run(cmd, args) {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { maxBuffer: 1024 * 1024 * 64 }, (err, stdout, stderr) =>
      err ? reject(new Error(stderr || err.message)) : resolve(stdout))
  })
}

async function runJob(jobId, rawPath) {
  await supabase.from('lidar_jobs').update({ status: 'processing', updated_at: new Date().toISOString() }).eq('id', jobId)

  const work = await fsp.mkdtemp(path.join(os.tmpdir(), 'lidar-'))
  const inFile = path.join(work, path.basename(rawPath))
  const outDir = path.join(work, 'octree')

  // 1. download LAZ
  const { data, error } = await supabase.storage.from(RAW_BUCKET).download(rawPath)
  if (error) throw error
  await fsp.writeFile(inFile, Buffer.from(await data.arrayBuffer()))

  // 2. convert to Potree octree
  await run('/opt/potree/PotreeConverter', [inFile, '-o', outDir])

  // 3. upload octree files
  const files = await fsp.readdir(outDir)
  for (const f of files) {
    const buf = await fsp.readFile(path.join(outDir, f))
    const up = await supabase.storage.from(OCTREE_BUCKET).upload(`${jobId}/${f}`, buf, { upsert: true })
    if (up.error) throw up.error
  }

  // 4. mark ready
  await supabase.from('lidar_jobs').update({
    status: 'ready', octree_path: `${jobId}/metadata.json`, updated_at: new Date().toISOString(),
  }).eq('id', jobId)

  await fsp.rm(work, { recursive: true, force: true })
  console.log('job ready', jobId)
}

app.listen(PORT, () => console.log('lidar worker on', PORT))
