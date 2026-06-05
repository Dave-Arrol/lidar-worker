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
const VIEW_MAX_POINTS = parseInt(clean(process.env.VIEW_MAX_POINTS) || '30000000', 10)
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
  // Large clouds (multi-GB) over Supabase's CDN occasionally drop the TLS
  // connection mid-stream ("other side closed"). Retry with an HTTP Range
  // header, resuming from the bytes already on disk rather than starting over.
  const MAX_ATTEMPTS = 8
  let offset = 0
  let total = null
  try { await fsp.rm(dest, { force: true }) } catch {}

  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    const { data: signed, error } = await supabase.storage.from(bucket).createSignedUrl(rawPath, 3600)
    if (error || !signed?.signedUrl) {
      // Right after a very large upload the object can be briefly invisible. If it
      // never appears, the upload was likely rejected (e.g. the bucket size limit).
      if (attempt >= MAX_ATTEMPTS)
        throw new Error(`raw cloud not found at ${bucket}/${rawPath} — the upload may have been rejected (check the ${bucket} bucket file-size limit). [${error?.message || 'no signed url'}]`)
      await new Promise(r => setTimeout(r, 1500 * attempt))
      continue
    }

    const headers = offset > 0 ? { Range: `bytes=${offset}-` } : {}
    const resp = await fetch(signed.signedUrl, { headers })
    if (!(resp.status === 200 || resp.status === 206) || !resp.body) {
      throw new Error(`download failed: ${resp.status}`)
    }
    // Resolve total size from Content-Range (resume) or Content-Length (fresh).
    if (total == null) {
      const cr = resp.headers.get('content-range')
      const cl = resp.headers.get('content-length')
      if (cr && cr.includes('/')) total = parseInt(cr.split('/')[1], 10)
      else if (cl && offset === 0) total = parseInt(cl, 10)
    }
    // Server ignored our Range and sent the whole file again — restart clean.
    if (offset > 0 && resp.status === 200) { offset = 0; try { await fsp.rm(dest, { force: true }) } catch {} }

    try {
      await new Promise((resolve, reject) => {
        const out = fs.createWriteStream(dest, { flags: offset > 0 ? 'a' : 'w' })
        const reader = resp.body.getReader()
        const pump = () => reader.read().then(({ done, value }) => {
          if (done) { out.end(); return }
          out.write(Buffer.from(value), (e) => {
            if (e) return reject(e)
            offset += value.byteLength   // only advance once flushed, so resume aligns
            pump()
          })
        }).catch(reject)
        out.on('finish', resolve); out.on('error', reject); pump()
      })
      // Stream ended cleanly but short (server closed without erroring) — resume.
      if (total != null && offset < total) {
        if (attempt >= MAX_ATTEMPTS) throw new Error(`download incomplete: ${offset}/${total} bytes`)
        await new Promise(r => setTimeout(r, 1000 * attempt))
        continue
      }
      return
    } catch (e) {
      if (attempt >= MAX_ATTEMPTS) throw e
      console.error(`download attempt ${attempt} dropped at ${offset} bytes (${e.message}); resuming...`)
      await new Promise(r => setTimeout(r, 1000 * attempt))   // linear backoff
    }
  }
}

const UPLOAD_MIME = {
  '.json': 'application/json', '.geojson': 'application/geo+json',
  '.tif': 'image/tiff', '.tiff': 'image/tiff', '.csv': 'text/csv',
  '.bin': 'application/octet-stream', '.las': 'application/octet-stream',
  '.laz': 'application/octet-stream', '.hrc': 'application/octet-stream',
}
async function uploadFile(bucket, key, filePath, upsert = true) {
  // Stream from disk via a Blob. fsp.readFile() buffers the whole file, and Node
  // can't hold a Buffer >2 GiB — octree.bin for large clouds blows past that.
  // openAsBlob is lazily read, and 3-4 GB stays under S3's 5 GB single-PUT limit.
  const blob = await fs.openAsBlob(filePath)
  const contentType = UPLOAD_MIME[path.extname(key).toLowerCase()] || 'application/octet-stream'
  const { error } = await supabase.storage.from(bucket).upload(key, blob, { upsert, contentType })
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

  // Build the octree from a thinned copy — the viewer doesn't need full density,
  // so a smaller cloud means a far smaller/faster octree with much less RAM.
  // Analysis is unaffected: /analyse downloads the dense cloud separately. Falls
  // back to the dense file if thinning fails for any reason.
  const thinFile = path.join(work, 'view.las')
  let convertInput = inFile
  try {
    await run('python3', ['/app/scripts/thin_las.py', '--input', inFile, '--output', thinFile, '--max-points', String(VIEW_MAX_POINTS)])
    convertInput = thinFile
  } catch (e) {
    console.error('thinning failed, converting full cloud', e)
  }
  await run('/opt/potree/PotreeConverter', [convertInput, '-o', outDir, '--encoding', 'BROTLI'])
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

// Mirror a tree/stem GeoJSON into the analyse-once feature store (PostGIS).
// Clears the prior set for this estate+cloud+source, then batch-inserts points.
async function ingestFeatures(table, source, geojsonPath, estateId, cloudJobId) {
  let gj
  try { gj = JSON.parse(await fsp.readFile(geojsonPath, 'utf8')) } catch { return 0 }
  const feats = gj.features || []
  await supabase.from(table).delete().eq('estate_id', estateId).eq('cloud_job_id', cloudJobId).eq('source', source)
  const rows = []
  for (const f of feats) {
    const c = f.geometry && f.geometry.coordinates
    if (!Array.isArray(c)) continue
    const p = f.properties || {}
    const row = {
      estate_id: estateId, cloud_job_id: cloudJobId, source,
      tree_id: p.tree_id != null ? Math.trunc(Number(p.tree_id)) : null,
      height_m: typeof p.height_m === 'number' ? p.height_m : null,
      geom: `SRID=4326;POINT(${c[0]} ${c[1]})`,
    }
    if (table === 'analysis_stems') row.dbh_cm = typeof p.dbh_cm === 'number' ? p.dbh_cm : null
    rows.push(row)
  }
  for (let i = 0; i < rows.length; i += 2000) {
    const { error } = await supabase.from(table).insert(rows.slice(i, i + 2000))
    if (error) throw error
  }
  return rows.length
}

async function runAnalyses(cloudJobId, ids) {
  const { data: job } = await supabase.from('lidar_jobs').select('raw_path,site_id').eq('id', cloudJobId).single()
  if (!job) throw new Error('cloud job not found: ' + cloudJobId)
  const chain = resolveChain(ids)

  // ONE shared workspace: every stage reads/writes named artifacts here, so a stage
  // like DBH can consume tree_candidates.las + ground.csv + treetops.csv from earlier stages.
  const work = await fsp.mkdtemp(path.join(os.tmpdir(), 'analyse-'))
  const ext = path.extname(job.raw_path) || '.las'
  const ctx = { dir: work, cloud: path.join(work, 'cloud' + ext), f: (name) => path.join(work, name) }
  // Insert the whole resolved plan as 'queued' up front so the portal shows the full
  // pipeline immediately — even while the (potentially large) cloud is still downloading.
  const planIds = []
  for (const a of chain) {
    const { data: row } = await supabase.from('lidar_analyses').insert({
      cloud_job_id: cloudJobId, site_id: job.site_id, type: a.id, status: 'queued',
    }).select('id').single()
    planIds.push(row.id)
  }

  await streamDownload(RAW_BUCKET, job.raw_path, ctx.cloud)

  let featuresIngested = false
  for (let i = 0; i < chain.length; i++) {
    const a = chain[i]
    const analysisId = planIds[i]
    // updated_at marks the processing-start time (used for the live elapsed timer).
    await supabase.from('lidar_analyses').update({
      status: 'processing', updated_at: new Date().toISOString(),
    }).eq('id', analysisId)

    try {
      // matplotlib headless; scripts read prior artifacts straight from the shared dir
      await run('python3', [path.join('/app/scripts', a.script), ...a.args(ctx)])
      const produced = []
      let summary = {}
      for (const out of a.outputs) {
        const fpath = ctx.f(out.file)
        if (out.role === 'summary') { summary = JSON.parse(await fsp.readFile(fpath, 'utf8')) }
        else if (out.role === 'raster') produced.push(await handleRaster(fpath, out, job.site_id, a.id))
        else if (out.role === 'vector') {
          produced.push(await handleVector(fpath, out, job.site_id, a.id))
          // Mirror tree/stem vectors into the feature store. Best-effort: never fails
          // the analysis if the hierarchy schema isn't applied yet.
          try {
            if (out.kind === 'treetops') { await ingestFeatures('analysis_trees', 'treetops', fpath, job.site_id, cloudJobId); featuresIngested = true }
            else if (out.kind === 'dbh') { await ingestFeatures('analysis_stems', 'dbh', fpath, job.site_id, cloudJobId); featuresIngested = true }
          } catch (e) { console.error('feature ingest failed (non-fatal):', out.kind, String(e).slice(0, 200)) }
        }
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
  if (featuresIngested) {
    // Stamp each tree/stem with the compartment that contains it (one SQL pass).
    try { await supabase.rpc('tag_analysis_geometry', { p_estate: job.site_id }) }
    catch (e) { console.error('tag_analysis_geometry failed (non-fatal):', String(e).slice(0, 200)) }
  }
  await fsp.rm(work, { recursive: true, force: true })
}

app.listen(PORT, () => console.log('lidar worker on', PORT))
