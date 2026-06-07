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
// S3 (AWS) — the heavy-data lane: the worker reads uploaded clouds from here and writes
// COPCs back here. Credentials come from AWS_ACCESS_KEY_ID/SECRET env (or a task role).
const S3_BUCKET = clean(process.env.S3_BUCKET) || 'arrol-lidar'
const AWS_REGION = clean(process.env.AWS_REGION) || 'eu-west-2'
// Density to extract from a COPC for analysis (metres). The COPC octree lets us pull a
// coarser level of detail than full density, so we never load a 100M-point cloud whole.
// 0.2 m ≈ a few million points for a typical drone site — ample for DTM/CHM, safe on RAM.
const ANALYSE_RES = clean(process.env.ANALYSE_COPC_RESOLUTION) || ''  // empty = full density (no reduction); set only to rescue a huge cloud

// PROJ/GDAL data paths for the conda-installed geo tools (untwine, and later pdal).
// These binaries live on PATH but were never conda-activated, so PROJ can't locate its
// database and spews "Open of /opt/pdal/share/proj failed". We point ONLY these tools at
// the conda database, scoped through the subprocess env (NOT a global var), because the
// image carries THREE separate PROJ stacks: conda's for untwine/pdal, rasterio's bundled
// copy for the Python analyses, and apt's for the GDAL raster steps. A global PROJ_DATA
// would feed the wrong-version database to the other two and break them.
const CONDA_PREFIX = clean(process.env.CONDA_PREFIX) || '/opt/pdal'
const GEO_ENV = {
  ...process.env,
  PROJ_DATA: `${CONDA_PREFIX}/share/proj`,
  PROJ_LIB:  `${CONDA_PREFIX}/share/proj`,   // older PROJ reads PROJ_LIB; harmless on PROJ 9
  GDAL_DATA: `${CONDA_PREFIX}/share/gdal`,
}

console.log('ENV -> URL:', SUPABASE_URL ? 'set' : 'MISSING', '| KEY:', SUPABASE_SERVICE_ROLE_KEY ? 'set' : 'MISSING', '| S3:', S3_BUCKET)
// Guard missing Supabase config so the worker still boots — and /health + /ingest stay
// usable — even before the app-DB env vars are wired in.
const supabase = (SUPABASE_URL && SUPABASE_SERVICE_ROLE_KEY)
  ? createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
  : null
const app = express()
app.use(express.json())

app.get('/health', (_req, res) => res.json({ ok: true }))
app.get('/registry', (req, res) => {
  if (req.headers['x-worker-secret'] !== WORKER_SECRET) return res.status(401).json({ error: 'unauthorized' })
  res.json(publicRegistry())
})

// ---- cloud ingest: uploaded .las/.laz in S3 -> COPC in S3 ----
// The "user uploads, cloud handles it" path. untwine reads both LAS and LAZ and is
// out-of-core (low RAM), but it needs scratch + the COPC output on the task's ephemeral
// disk, so bump the task's ephemeral storage for clouds bigger than a few GB.
app.post('/ingest', (req, res) => {
  if (req.headers['x-worker-secret'] !== WORKER_SECRET) return res.status(401).json({ error: 'unauthorized' })
  const { key, outKey, jobId } = req.body || {}
  if (!key) return res.status(400).json({ error: 'key required — the S3 key of the uploaded .las/.laz' })
  res.status(202).json({ accepted: true, key })
  ingestCopc(key, outKey, jobId).catch(async (e) => {
    console.error('[ingest] failed', key, String(e))
    // Surface the failure to the portal so the panel can show it (best-effort).
    if (jobId && supabase) {
      try {
        await supabase.from('lidar_jobs')
          .update({ status: 'failed', error: String(e).slice(0, 500), updated_at: new Date().toISOString() })
          .eq('id', jobId)
      } catch (_) {}
    }
  })
})

async function ingestCopc(key, outKeyArg, jobId) {
  const s3 = (k) => `s3://${S3_BUCKET}/${k}`
  const region = ['--region', AWS_REGION]
  const inExt = (path.extname(key) || '.las').toLowerCase()
  const base = path.basename(key).replace(/\.(la[sz])$/i, '')
  const outKey = outKeyArg || `copc/${base}.copc.laz`

  const work = await fsp.mkdtemp(path.join(os.tmpdir(), 'ingest-'))
  const inFile = path.join(work, 'in' + inExt)
  const outFile = path.join(work, 'out.copc.laz')
  const tmpDir = path.join(work, 'untwine_tmp')
  try {
    console.log('[ingest] download', s3(key))
    await run('aws', ['s3', 'cp', s3(key), inFile, '--no-progress', ...region])
    console.log('[ingest] untwine -> COPC')
    await run('untwine', ['-i', inFile, '-o', outFile, '--temp_dir', tmpDir], { env: GEO_ENV })
    console.log('[ingest] upload', s3(outKey))
    await run('aws', ['s3', 'cp', outFile, s3(outKey), '--no-progress', ...region])
    console.log('[ingest] done ->', outKey)
    // Mark the job COPC-ready so the portal can pick it up. Wrapped: the COPC is already
    // safely in S3, so a DB hiccup here must not flip the job to 'failed'.
    if (jobId && supabase) {
      try {
        await supabase.from('lidar_jobs')
          .update({ copc_path: outKey, status: 'ready', updated_at: new Date().toISOString() })
          .eq('id', jobId)
      } catch (e2) { console.error('[ingest] row update failed (COPC is fine):', String(e2)) }
    }
  } finally {
    await fsp.rm(work, { recursive: true, force: true }).catch(() => {})
  }
}

function run(cmd, args, opts = {}) {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { maxBuffer: 1024 * 1024 * 64, ...opts }, (err, stdout, stderr) => {
      if (stdout) console.log('[stdout]', stdout)
      if (stderr) console.log('[stderr]', stderr)
      err ? reject(new Error([stderr, stdout, err.message].filter(Boolean).join(' | '))) : resolve(stdout)
    })
  })
}

async function streamDownload(bucket, rawPath, dest) {
  // Multi-GB clouds over Supabase's CDN get killed at a fixed connection-duration
  // limit (~100s), and the resume Range on a single long stream was being ignored
  // (server replies 200 with the whole file → restart-from-zero loop, stuck forever
  // at the same byte). Instead we pull the object in explicit fixed-size Range chunks:
  // each request is short-lived (never hits the time limit), deterministic, and
  // retried on its own. Chunks are written to disk in order, bounded memory.
  const CHUNK = 128 * 1024 * 1024   // 128 MB per request — shorter requests drop less often
  const MAX_RETRY = 10

  const sign = async () => {
    const { data, error } = await supabase.storage.from(bucket).createSignedUrl(rawPath, 3600)
    if (error || !data?.signedUrl)
      throw new Error(`raw cloud not found at ${bucket}/${rawPath} — the upload may have been rejected (check the ${bucket} bucket file-size limit). [${error?.message || 'no signed url'}]`)
    return data.signedUrl
  }

  // Probe total size + whether the endpoint honours Range (Content-Range on a 1-byte GET).
  let total = null, rangeOk = false
  {
    const r = await fetch(await sign(), { headers: { Range: 'bytes=0-0' } })
    const cr = r.headers.get('content-range')          // "bytes 0-0/TOTAL"
    if (cr && cr.includes('/')) { total = parseInt(cr.split('/')[1], 10); rangeOk = true }
    else { const cl = r.headers.get('content-length'); if (cl) total = parseInt(cl, 10) }
    try { await r.body?.cancel?.() } catch {}
  }
  if (!total || !Number.isFinite(total)) throw new Error(`could not determine size of ${bucket}/${rawPath}`)

  await fsp.rm(dest, { force: true }).catch(() => {})
  const out = fs.createWriteStream(dest, { flags: 'w' })
  const writeBody = async (body) => {
    const reader = body.getReader()
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      if (!out.write(Buffer.from(value))) await new Promise(res => out.once('drain', res))  // honour backpressure
    }
  }

  try {
    if (!rangeOk) {
      // No Range support — fall back to a single stream (best effort).
      const resp = await fetch(await sign())
      if (!resp.ok || !resp.body) throw new Error(`download failed: ${resp.status}`)
      await writeBody(resp.body)
    } else {
      for (let start = 0; start < total; start += CHUNK) {
        const end = Math.min(start + CHUNK, total) - 1
        let okChunk = false
        for (let attempt = 1; attempt <= MAX_RETRY && !okChunk; attempt++) {
          try {
            const resp = await fetch(await sign(), { headers: { Range: `bytes=${start}-${end}` } })
            if (resp.status !== 206 || !resp.body) throw new Error(`status ${resp.status}`)
            await writeBody(resp.body)
            okChunk = true
          } catch (e) {
            if (attempt >= MAX_RETRY) throw new Error(`download chunk ${start}-${end} failed: ${e.message}`)
            console.error(`chunk ${start}-${end} attempt ${attempt} failed (${e.message}); retrying...`)
            await new Promise(r => setTimeout(r, 1500 * attempt))
          }
        }
      }
    }
  } finally {
    await new Promise((res, rej) => out.end(err => err ? rej(err) : res()))
  }
}

const UPLOAD_MIME = {
  '.json': 'application/json', '.geojson': 'application/geo+json',
  '.tif': 'image/tiff', '.tiff': 'image/tiff', '.csv': 'text/csv',
  '.bin': 'application/octet-stream', '.las': 'application/octet-stream',
  '.laz': 'application/octet-stream', '.hrc': 'application/octet-stream',
}
// Turn any thrown value (incl. opaque Supabase/Postgres error objects, which
// stringify to "[object Object]") into something legible for logs.
function errMsg(e) {
  if (!e) return 'unknown'
  if (typeof e === 'string') return e
  if (e.message) return String(e.message)
  if (e.code || e.details || e.hint) return [e.code, e.details, e.hint].filter(Boolean).join(' | ')
  try { return JSON.stringify(e) } catch { return String(e) }
}

async function uploadFile(bucket, key, filePath, upsert = true) {
  // Stream from disk via a Blob. fsp.readFile() buffers the whole file, and Node
  // can't hold a Buffer >2 GiB — octree.bin for large clouds blows past that.
  // openAsBlob is lazily read, and 3-4 GB stays under S3's 5 GB single-PUT limit.
  const contentType = UPLOAD_MIME[path.extname(key).toLowerCase()] || 'application/octet-stream'
  // Supabase storage's gateway intermittently 502s on otherwise-fine uploads.
  // A 5xx/429 is transient, so retry with backoff before giving up. (Genuine
  // size-limit failures on huge clouds still need the S3 output lane — separate.)
  const attempts = 4
  for (let attempt = 1; attempt <= attempts; attempt++) {
    const blob = await fs.openAsBlob(filePath)   // re-open per try; a Blob is re-readable but this is safest
    const { error } = await supabase.storage.from(bucket).upload(key, blob, { upsert, contentType })
    if (!error) return
    const status = Number(error.statusCode || error.status || 0)
    const retryable = status === 0 || status === 429 || (status >= 500 && status <= 599)
    if (attempt === attempts || !retryable) throw error
    const backoff = Math.min(8000, 500 * 2 ** (attempt - 1)) + Math.floor(Math.random() * 300)
    console.warn(`upload ${key} failed (status ${status}), retry ${attempt}/${attempts - 1} in ${backoff}ms: ${errMsg(error)}`)
    await new Promise(r => setTimeout(r, backoff))
  }
}

// ---- existing octree conversion (unchanged behaviour) ----
app.post('/process', (req, res) => {
  if (req.headers['x-worker-secret'] !== WORKER_SECRET) return res.status(401).json({ error: 'unauthorized' })
  const { jobId, rawPath } = req.body || {}
  if (!jobId || !rawPath) return res.status(400).json({ error: 'jobId and rawPath required' })
  res.status(202).json({ accepted: true })
  convertJob(jobId, rawPath).catch((e) => {
    // Octree is the optional 3D-view artifact — log the failure but DON'T touch the
    // cloud's status, so analysis (which uses the raw LAS) stays available regardless.
    console.error('octree build failed', jobId, e)
  })
})

async function convertJob(jobId, rawPath) {
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
  // Octree-only: set the viewer pointer. Status is owned by upload (ready) / analysis,
  // never by the octree build, so a viewing failure can't block analysis.
  await supabase.from('lidar_jobs').update({ octree_path: `${jobId}/metadata.json`, updated_at: new Date().toISOString() }).eq('id', jobId)
  await fsp.rm(work, { recursive: true, force: true })
  console.log('octree ready', jobId)
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
      : output.mode === 'water'
        ? [[lo, '198 230 245'], [lo + 0.5 * rng, '66 146 198'], [hi, '8 48 107']]
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

// Tariff produces a per-tree merchantable volume (keyed by tree_id). Attach it to the
// trees already ingested from treetops, matched by tree_id, via one set-based RPC.
async function ingestTreeVolumes(geojsonPath, estateId, cloudJobId) {
  let gj
  try { gj = JSON.parse(await fsp.readFile(geojsonPath, 'utf8')) } catch { return 0 }
  const vols = []
  for (const f of (gj.features || [])) {
    const p = f.properties || {}
    const t = p.tree_id != null ? Math.trunc(Number(p.tree_id)) : null
    const v = typeof p.merch_volume_m3 === 'number' ? p.merch_volume_m3 : null
    if (t != null && v != null) vols.push({ t, v })
  }
  if (!vols.length) return 0
  const { data, error } = await supabase.rpc('set_tree_volumes', {
    p_estate: estateId, p_cloud_job: cloudJobId, p_vols: vols,
  })
  if (error) throw error
  return typeof data === 'number' ? data : vols.length
}

async function runAnalyses(cloudJobId, ids) {
  const { data: job } = await supabase.from('lidar_jobs').select('raw_path,site_id,copc_path,storage').eq('id', cloudJobId).single()
  if (!job) throw new Error('cloud job not found: ' + cloudJobId)
  const chain = resolveChain(ids)

  // ONE shared workspace: every stage reads/writes named artifacts here, so a stage
  // like DBH can consume tree_candidates.las + ground.csv + treetops.csv from earlier stages.
  const work = await fsp.mkdtemp(path.join(os.tmpdir(), 'analyse-'))
  // S3/COPC jobs: the cloud lives in S3 (we pull the COPC, a valid LAZ 1.4). Legacy jobs:
  // the raw LAS streams from Supabase.
  const isS3 = job.storage === 's3' || !!job.copc_path
  const ext = isS3 ? '.las' : (path.extname(job.raw_path) || '.las')
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

  // Materialise the cloud into the workspace.
  if (isS3) {
    // Analyse the ORIGINAL uploaded LAS at full fidelity — the same bytes the Railway
    // pipeline used, and what the legacy path below still does. The COPC is a viewing/
    // subsetting derivative; routing analysis through copc_clip was re-quantising and
    // thinning the cloud, which starves DBH ring-fitting. So source the raw upload.
    // Fall back to clipping the COPC only when there's no raw LAS available, or when
    // ANALYSE_COPC_RESOLUTION is set to rescue a cloud too big to load whole.
    const rawKey = job.raw_path
    const rawIsCopc = !!rawKey && /\.copc\./i.test(rawKey)
    const useRaw = !!rawKey && !rawIsCopc && !ANALYSE_RES
    if (useRaw) {
      console.log('[analyse] source: original LAS (full fidelity)', rawKey)
      await run('aws', ['s3', 'cp', `s3://${S3_BUCKET}/${rawKey}`, ctx.cloud, '--no-progress', '--region', AWS_REGION])
    } else {
      const srcKey = job.copc_path || job.raw_path
      const copcTmp = path.join(work, 'src.copc.laz')
      console.log('[analyse] source: COPC extract', srcKey, ANALYSE_RES ? `@ ${ANALYSE_RES} m` : '(full density)')
      await run('aws', ['s3', 'cp', `s3://${S3_BUCKET}/${srcKey}`, copcTmp, '--no-progress', '--region', AWS_REGION])
      const clipArgs = ['--input', copcTmp, '--out', ctx.cloud, '--stats']
      if (ANALYSE_RES) clipArgs.push('--resolution', String(ANALYSE_RES))
      await run('python3', [path.join('/app/scripts', 'copc_clip.py'), ...clipArgs], { env: GEO_ENV })
    }
  } else {
    await streamDownload(RAW_BUCKET, job.raw_path, ctx.cloud)
  }

  // Write the estate's compartment polygons into the workspace so the tariff stage can
  // refit a tariff per compartment (point-in-polygon). Best-effort: tariff.py falls back
  // to a single stand tariff if this file is empty or missing.
  try {
    const { data: comps } = await supabase.from('compartments')
      .select('id,reference,name,boundary,area_hectares,attributes').eq('site_id', job.site_id)
    const speciesOf = (attrs) => {
      if (!attrs || typeof attrs !== 'object') return null
      for (const k of ['Species', 'species', 'SPECIES', 'Spp', 'spp', 'Tree Species', 'tree_species']) {
        const v = attrs[k]
        if (v != null && String(v).trim()) return String(v).trim()
      }
      return null
    }
    const features = (comps || []).filter(c => c.boundary).map(c => ({
      type: 'Feature',
      properties: { id: c.id, ref: c.reference || c.name || String(c.id), area_hectares: c.area_hectares, species: speciesOf(c.attributes) },
      geometry: c.boundary,
    }))
    await fsp.writeFile(ctx.f('compartments.geojson'), JSON.stringify({ type: 'FeatureCollection', features }))
  } catch (e) { console.error('compartments fetch failed (non-fatal):', errMsg(e).slice(0, 300)) }

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
            else if (out.kind === 'volume') { await ingestTreeVolumes(fpath, job.site_id, cloudJobId) }
          } catch (e) { console.error('feature ingest failed (non-fatal):', out.kind, errMsg(e).slice(0, 300)) }
        }
        else if (out.role === 'points') {
          // Octree is only for the optional 3D viewer. PotreeConverter hard-crashes
          // on a degenerate/empty cloud (e.g. a stage that fit zero stems). Don't let
          // that abort the chain — skip this octree and keep going so the rest of the
          // run, the feature ingest, and the final tagging all still complete.
          try { produced.push(await handlePoints(fpath, out, analysisId)) }
          catch (e) { console.error('octree build failed (non-fatal):', out.file, errMsg(e).slice(0, 300)) }
        }
        else if (out.role === 'table')  produced.push(await handleTable(fpath, out, analysisId))
      }
      await supabase.from('lidar_analyses').update({
        status: 'ready', summary, outputs: produced, updated_at: new Date().toISOString(),
      }).eq('id', analysisId)
      console.log('analysis ready', a.id, analysisId)
    } catch (e) {
      await supabase.from('lidar_analyses').update({
        status: 'failed', error: errMsg(e).slice(0, 500), updated_at: new Date().toISOString(),
      }).eq('id', analysisId)
      throw e
    }
  }
  if (featuresIngested) {
    // Stamp each tree/stem with the compartment that contains it (one SQL pass).
    try { await supabase.rpc('tag_analysis_geometry', { p_estate: job.site_id }) }
    catch (e) { console.error('tag_analysis_geometry failed (non-fatal):', errMsg(e).slice(0, 300)) }
  }
  await fsp.rm(work, { recursive: true, force: true })
}

app.listen(PORT, () => console.log('lidar worker on', PORT))