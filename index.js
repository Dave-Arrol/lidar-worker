const express = require('express')
const { createClient } = require('@supabase/supabase-js')
const { execFile } = require('child_process')
const fs = require('fs')
const fsp = require('fs/promises')
const path = require('path')
const os = require('os')
const { publicRegistry, resolveChain } = require('./registry')

function clean(v) { return v ? v.trim().replace(/^['"]|['"]$/g, '').trim() : '' }
const SUPABASE_URL = clean(process.env.SUPABASE_URL)
const SUPABASE_SERVICE_ROLE_KEY = clean(process.env.SUPABASE_SERVICE_ROLE_KEY)
const WORKER_SECRET = clean(process.env.WORKER_SECRET)
const RAW_BUCKET = clean(process.env.RAW_BUCKET) || 'lidar-raw'
const OCTREE_BUCKET = clean(process.env.OCTREE_BUCKET) || 'lidar-octree'
const LAYERS_BUCKET = clean(process.env.LAYERS_BUCKET) || 'site-layers'
const RESULTS_BUCKET = clean(process.env.RESULTS_BUCKET) || 'lidar-results'
const PORT = clean(process.env.PORT) || 8080

console.log('ENV -> URL:', SUPABASE_URL ? 'set' : 'MISSING', '| KEY:', SUPABASE_SERVICE_ROLE_KEY ? 'set' : 'MISSING')
const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
const app = express()
app.use(express.json())

app.get('/health', (_req, res) => res.json({ ok: true }))
app.get('/registry', (req, res) => {
  if (req.headers['x-worker-secret'] !== WORKER_SECRET) return res.status(401).json({ error: 'unauthorized' })
  res.json(publicRegistry())
})

function run(cmd, args) {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { maxBuffer: 1024 * 1024 * 64 }, (err, stdout, stderr) => {
      if (stdout) console.log('[stdout]', stdout)
      if (stderr) console.log('[stderr]', stderr)
      err ? reject(new Error([stderr, stdout, err.message].filter(Boolean).join(' | '))) : resolve(stdout)
    })
  })
}

async function streamDownload(bucket, rawPath, dest) {
  const { data: signed, error } = await supabase.storage.from(bucket).createSignedUrl(rawPath, 3600)
  if (error) throw error
  const resp = await fetch(signed.signedUrl)
  if (!resp.ok || !resp.body) throw new Error(`download failed: ${resp.status}`)
  await new Promise((resolve, reject) => {
    const out = fs.createWriteStream(dest)
    const reader = resp.body.getReader()
    const pump = () => reader.read().then(({ done, value }) => {
      if (done) { out.end(); return }
      out.write(Buffer.from(value), (e) => e ? reject(e) : pump())
    }).catch(reject)
    out.on('finish', resolve); out.on('error', reject); pump()
  })
}

async function uploadFile(bucket, key, filePath, upsert = true) {
  const buf = await fsp.readFile(filePath)
  const { error } = await supabase.storage.from(bucket).upload(key, buf, { upsert })
  if (error) throw error
}

// ---- existing octree conversion (unchanged behaviour) ----
app.post('/process', (req, res) => {
  if (req.headers['x-worker-secret'] !== WORKER_SECRET) return res.status(401).json({ error: 'unauthorized' })
  const { jobId, rawPath } = req.body || {}
  if (!jobId || !rawPath) return res.status(400).json({ error: 'jobId and rawPath required' })
  res.status(202).json({ accepted: true })
  convertJob(jobId, rawPath).catch(async (e) => {
    console.error('job failed', jobId, e)
    await supabase.from('lidar_jobs').update({ status: 'failed', error: String(e).slice(0, 500), updated_at: new Date().toISOString() }).eq('id', jobId)
  })
})

async function convertJob(jobId, rawPath) {
  await supabase.from('lidar_jobs').update({ status: 'processing', updated_at: new Date().toISOString() }).eq('id', jobId)
  const work = await fsp.mkdtemp(path.join(os.tmpdir(), 'lidar-'))
  const inFile = path.join(work, path.basename(rawPath))
  const outDir = path.join(work, 'octree')
  await streamDownload(RAW_BUCKET, rawPath, inFile)
  await run('/opt/potree/PotreeConverter', [inFile, '-o', outDir, '--encoding', 'BROTLI'])
  for (const f of await fsp.readdir(outDir)) await uploadFile(OCTREE_BUCKET, `${jobId}/${f}`, path.join(outDir, f))
  await supabase.from('lidar_jobs').update({ status: 'ready', octree_path: `${jobId}/metadata.json`, updated_at: new Date().toISOString() }).eq('id', jobId)
  await fsp.rm(work, { recursive: true, force: true })
  console.log('job ready', jobId)
}

// ---- analysis framework ----
app.post('/analyse', (req, res) => {
  if (req.headers['x-worker-secret'] !== WORKER_SECRET) return res.status(401).json({ error: 'unauthorized' })
  const { cloudJobId, analyses } = req.body || {}
  if (!cloudJobId || !Array.isArray(analyses) || !analyses.length)
    return res.status(400).json({ error: 'cloudJobId and analyses[] required' })
  res.status(202).json({ accepted: true })
  runAnalyses(cloudJobId, analyses).catch(e => console.error('analyse error', e))
})

async function gdalBands(file) {
  const out = await run('gdalinfo', ['-json', '-mm', file])
  return JSON.parse(out)
}

// raster output -> reproject + COG + register as a site_layers map overlay
async function handleRaster(file, output, siteId, type) {
  const work = path.dirname(file)
  const info = await gdalBands(file)
  let toWarp = file, comp = ['-co', 'COMPRESS=DEFLATE']
  if (info.bands.length === 1) {
    const b = info.bands[0], lo = b.computedMin, hi = b.computedMax, rng = (hi - lo) || 1
    const stops =
      output.mode === 'grey'
        ? [[lo, '0 0 0'], [hi, '255 255 255']]
      : output.mode === 'slope' || output.mode === 'density'
        ? [[lo, '38 130 76'], [lo + 0.33 * rng, '232 212 77'], [lo + 0.66 * rng, '214 130 54'], [hi, '200 50 40']]
      : output.mode === 'terrain'
        ? [[lo, '46 110 70'], [lo + 0.30 * rng, '150 180 110'], [lo + 0.55 * rng, '224 206 144'],
           [lo + 0.80 * rng, '150 110 75'], [hi, '240 240 240']]
        : [[lo, '43 106 63'], [lo + 0.25 * rng, '116 164 75'], [lo + 0.5 * rng, '232 212 77'],
           [lo + 0.75 * rng, '168 106 51'], [hi, '245 245 245']]
    const ramp = path.join(work, 'ramp.txt')
    await fsp.writeFile(ramp, 'nv 0 0 0 0\n' + stops.map(s => `${s[0]} ${s[1]} 255`).join('\n') + '\n')
    const col = path.join(work, 'col.tif')
    await run('gdaldem', ['color-relief', file, ramp, col, '-alpha'])
    toWarp = col
  } else { comp = ['-co', 'COMPRESS=JPEG', '-co', 'QUALITY=85'] }
  const merc = path.join(work, 'merc.tif'), cog = path.join(work, `${type}_cog.tif`)
  await run('gdalwarp', ['-t_srs', 'EPSG:3857', '-r', 'bilinear', '-overwrite', toWarp, merc])
  await run('gdal_translate', [merc, cog, '-of', 'COG', '-co', 'OVERVIEWS=AUTO', ...comp])
  const key = `${siteId}/lidar-${type}-${Date.now()}.tif`
  await uploadFile(LAYERS_BUCKET, key, cog)
  await replaceLayer(siteId, output.name || type, {
    site_id: siteId, name: output.name || type, layer_type: output.kind || 'raster',
    storage_path: key, opacity: 1, visible: true, sort_order: 0,
  })
  return { role: 'raster', name: output.name || type, layer_type: output.kind || 'raster',
           bucket: LAYERS_BUCKET, path: key, added_to_map: true }
}

async function handlePoints(file, output, analysisId) {
  const octDir = path.join(path.dirname(file), `oct-${analysisId}`)
  await run('/opt/potree/PotreeConverter', [file, '-o', octDir, '--encoding', 'BROTLI'])
  for (const f of await fsp.readdir(octDir)) await uploadFile(OCTREE_BUCKET, `analysis-${analysisId}/${f}`, path.join(octDir, f))
  return { role: 'points', name: output.name, octree_path: `analysis-${analysisId}/metadata.json` }
}

async function handleTable(file, output, analysisId) {
  const key = `${analysisId}/${output.file}`
  await uploadFile(RESULTS_BUCKET, key, file)
  return { role: 'table', name: output.name, bucket: RESULTS_BUCKET, path: key }
}

// Upsert a map layer by (site, name): drop any prior version (row + file) so
// re-running an analysis replaces its layer instead of stacking duplicates.
async function replaceLayer(siteId, name, rowToInsert) {
  const { data: old } = await supabase.from('site_layers')
    .select('id,storage_path').eq('site_id', siteId).eq('name', name)
  if (old && old.length) {
    await supabase.storage.from(LAYERS_BUCKET).remove(old.map(o => o.storage_path)).catch(() => {})
    await supabase.from('site_layers').delete().in('id', old.map(o => o.id))
  }
  await supabase.from('site_layers').insert(rowToInsert)
}

// vector (GeoJSON, already EPSG:4326) -> register as a site_layers map overlay
async function handleVector(file, output, siteId, type) {
  const key = `${siteId}/lidar-${type}-${Date.now()}.geojson`
  await uploadFile(LAYERS_BUCKET, key, file)
  await replaceLayer(siteId, output.name || type, {
    site_id: siteId, name: output.name || type, layer_type: output.kind || 'points',
    storage_path: key, opacity: 1, visible: true, sort_order: 0,
  })
  return { role: 'vector', name: output.name || type, layer_type: output.kind || 'points',
           bucket: LAYERS_BUCKET, path: key, added_to_map: true }
}

async function runAnalyses(cloudJobId, ids) {
  const { data: job } = await supabase.from('lidar_jobs').select('raw_path,site_id').eq('id', cloudJobId).single()
  if (!job) throw new Error('cloud job not found')
  const chain = resolveChain(ids)

  // ONE shared workspace: every stage reads/writes named artifacts here, so a stage
  // like DBH can consume tree_candidates.las + ground.csv + treetops.csv from earlier stages.
  const work = await fsp.mkdtemp(path.join(os.tmpdir(), 'analyse-'))
  const ext = path.extname(job.raw_path) || '.las'
  const ctx = { dir: work, cloud: path.join(work, 'cloud' + ext), f: (name) => path.join(work, name) }
  await streamDownload(RAW_BUCKET, job.raw_path, ctx.cloud)

  for (const a of chain) {
    const { data: row } = await supabase.from('lidar_analyses').insert({
      cloud_job_id: cloudJobId, site_id: job.site_id, type: a.id, status: 'processing',
    }).select('id').single()
    const analysisId = row.id

    try {
      // matplotlib headless; scripts read prior artifacts straight from the shared dir
      await run('python3', [path.join('/app/scripts', a.script), ...a.args(ctx)])
      const produced = []
      let summary = {}
      for (const out of a.outputs) {
        const fpath = ctx.f(out.file)
        if (out.role === 'summary') { summary = JSON.parse(await fsp.readFile(fpath, 'utf8')) }
        else if (out.role === 'raster') produced.push(await handleRaster(fpath, out, job.site_id, a.id))
        else if (out.role === 'vector') produced.push(await handleVector(fpath, out, job.site_id, a.id))
        else if (out.role === 'points') produced.push(await handlePoints(fpath, out, analysisId))
        else if (out.role === 'table')  produced.push(await handleTable(fpath, out, analysisId))
      }
      await supabase.from('lidar_analyses').update({
        status: 'ready', summary, outputs: produced, updated_at: new Date().toISOString(),
      }).eq('id', analysisId)
      console.log('analysis ready', a.id, analysisId)
    } catch (e) {
      await supabase.from('lidar_analyses').update({
        status: 'failed', error: String(e).slice(0, 500), updated_at: new Date().toISOString(),
      }).eq('id', analysisId)
      throw e
    }
  }
  await fsp.rm(work, { recursive: true, force: true })
}

app.listen(PORT, () => console.log('lidar worker on', PORT))
