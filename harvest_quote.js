// harvest_quote.js — worker handler for the Harvest Planner (feature #11).
//
// Wraps the validated predict_coupe.py v1 tool: turns a coupe's stems into a
// costed quote (summary row) + per-stem predicted cycle (speed-map rows).
// Self-contained on purpose — it owns its Supabase (service-role) client and
// its own run()/S3 helpers so index.js needs no edits; run-once.js just adds a
// 'harvest' branch that require()s this module.
//
// PAYLOAD (JSON string in env, mirrors the other worker modes):
//   { quoteId, siteId?, source, stemsKey?, machineRate?, fuelPrice? }
//     source = 'coupe'  -> stems pulled from harvested_stems WHERE site_id = siteId
//     source = 'upload' -> stems CSV downloaded from s3://S3_BUCKET/<stemsKey>
//
// Results flow back through Supabase exactly like the LiDAR jobs: the quote row
// is updated in place (queued -> processing -> ready|failed) and the per-stem
// rows are bulk-inserted, so the portal's polling is unchanged.

const { createClient } = require('@supabase/supabase-js')
const { execFile } = require('child_process')
const fs = require('fs')
const fsp = require('fs/promises')
const os = require('os')
const path = require('path')

function clean(v) { return v ? String(v).trim().replace(/^['"]|['"]$/g, '').trim() : '' }

const SUPABASE_URL = clean(process.env.SUPABASE_URL)
const SUPABASE_SERVICE_ROLE_KEY = clean(process.env.SUPABASE_SERVICE_ROLE_KEY)
const S3_BUCKET = clean(process.env.S3_BUCKET) || 'arrol-lidar'
const AWS_REGION = clean(process.env.AWS_REGION) || 'eu-west-2'
const MODEL_PATH = clean(process.env.HARVEST_MODEL_PATH) || '/app/harvest/model.pkl'
const PREDICT_SCRIPT = clean(process.env.HARVEST_PREDICT_SCRIPT) || '/app/harvest/predict_coupe.py'
const EXTRACT_TERRAIN = clean(process.env.HARVEST_EXTRACT_TERRAIN) || '/app/harvest/extract_terrain.py'
const OS_TILES_SCRIPT = clean(process.env.HARVEST_OS_TILES) || '/app/harvest/os_tiles_for_stems.py'
const TERR50_PREFIX = clean(process.env.HARVEST_TERR50_PREFIX) || 'terr50_gagg_gb/'

const supabase = (SUPABASE_URL && SUPABASE_SERVICE_ROLE_KEY)
  ? createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
  : null

function run(cmd, args, opts = {}) {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { maxBuffer: 1024 * 1024 * 64, ...opts }, (err, stdout, stderr) => {
      if (stdout) console.log('[stdout]', stdout)
      if (stderr) console.log('[stderr]', stderr)
      err ? reject(new Error([stderr, stdout, err.message].filter(Boolean).join(' | '))) : resolve(stdout)
    })
  })
}

// ---- CSV helpers (small, dependency-free) ---------------------------------
function csvField(v) {
  if (v === null || v === undefined) return ''
  const s = String(v)
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}
function writeCsv(filePath, header, rows) {
  const lines = [header.join(',')]
  for (const r of rows) lines.push(header.map((h) => csvField(r[h])).join(','))
  return fsp.writeFile(filePath, lines.join('\n') + '\n', 'utf8')
}
function parseCsv(text) {
  // predict_coupe output is plain numeric + short label cells (no embedded commas
  // in the fields we consume); a simple split is sufficient and robust here.
  const lines = text.trim().split(/\r?\n/)
  if (!lines.length) return []
  const header = lines[0].split(',')
  return lines.slice(1).map((ln) => {
    const cells = ln.split(',')
    const o = {}
    header.forEach((h, i) => { o[h.trim()] = cells[i] })
    return o
  })
}

// ---- normalise harvested_stems.species to the model's expected label ------
// The model's species_code = (species == "Spruce"). harvested_stems stores raw
// vendor species strings ("Sitka spruce", "Norway Spruce", latin, codes), so
// fold anything spruce-like to "Spruce" and leave the rest as-is (-> non-spruce).
function normSpecies(raw) {
  const s = (raw || '').toString().toLowerCase()
  return s.includes('spruce') || s === 'ss' || s === 'ns' ? 'Spruce' : (raw || 'Other')
}

// ---- pull a coupe's stems from harvested_stems into a stems CSV -----------
async function stemsFromCoupe(siteId, csvPath) {
  const header = ['object_id', 'stem_key', 'dbh_cm', 'stem_volume_m3', 'species', 'lat', 'lon', 'n_logs', 'hour']
  const PAGE = 1000
  let from = 0
  let total = 0
  const rows = []
  for (;;) {
    const { data, error } = await supabase
      .from('harvested_stems')
      .select('id, stem_number, dbh_mm, volume_ob_m3, species, latitude, longitude, logs, felled_at')
      .eq('site_id', siteId)
      .range(from, from + PAGE - 1)
    if (error) throw new Error(`harvested_stems query failed: ${error.message}`)
    if (!data || !data.length) break
    for (const s of data) {
      const dbhCm = s.dbh_mm != null ? s.dbh_mm / 10 : null
      const nLogs = Array.isArray(s.logs) ? s.logs.length : null
      const hour = s.felled_at ? new Date(s.felled_at).getUTCHours() : null
      rows.push({
        object_id: siteId,
        stem_key: s.stem_number != null ? s.stem_number : s.id,
        dbh_cm: dbhCm,
        stem_volume_m3: s.volume_ob_m3,
        species: normSpecies(s.species),
        lat: s.latitude,
        lon: s.longitude,
        n_logs: nLogs,
        hour,
      })
    }
    total += data.length
    if (data.length < PAGE) break
    from += PAGE
  }
  if (!rows.length) throw new Error(`no stems found in harvested_stems for site ${siteId}`)
  await writeCsv(csvPath, header, rows)
  return total
}

// ---- aggregate predict_coupe's per-object summary rows into one quote ------
function aggregateSummary(summaryRows) {
  const num = (v) => (v === '' || v == null ? 0 : Number(v))
  let vol = 0, cutH = 0, machineH = 0, fuel = 0, cost = 0, nStems = 0
  let windblow = false
  for (const r of summaryRows) {
    vol += num(r.volume_m3)
    cutH += num(r.harvester_pmh0_h)
    machineH += num(r.total_machine_h)
    fuel += num(r.fuel_litres)
    cost += num(r.est_cost_gbp)
    nStems += num(r.n_stems)
    if (String(r.confidence || '').toUpperCase().includes('LOW')) windblow = true
  }
  const pmh0 = cutH > 0 ? vol / cutH : null
  const band = windblow ? 0.30 : 0.18
  return {
    n_stems: nStems,
    volume_m3: round(vol, 1),
    pmh0: pmh0 != null ? round(pmh0, 1) : null,
    pmh0_band_lo: pmh0 != null ? round(pmh0 * (1 - band), 1) : null,
    pmh0_band_hi: pmh0 != null ? round(pmh0 * (1 + band), 1) : null,
    machine_h: round(machineH, 1),
    fuel_l: round(fuel, 0),
    cost_gbp: round(cost, 0),
    cost_band_lo: round(cost * (1 - band), 0),
    cost_band_hi: round(cost * (1 + band), 0),
    confidence: windblow ? 'LOW (windblow)' : 'standard (+/-18%)',
  }
}
function round(v, dp) { const f = 10 ** dp; return Math.round(v * f) / f }

async function markFailed(quoteId, err) {
  if (!supabase || !quoteId) return
  await supabase.from('harvest_quote').update({
    status: 'failed', error: String(err).slice(0, 500),
    updated_at: new Date().toISOString(), completed_at: new Date().toISOString(),
  }).eq('id', quoteId)
}


// ---- forecast stems from LiDAR-detected trees (Stage C) -------------------
// For a coupe with no felled stems, quote from the LiDAR mensuration analysis:
// per-tree DBH + volume + position via the harvest_lidar_stems RPC. Species is
// a coupe-level assumption (the model only splits Spruce vs non-Spruce).
async function stemsFromLidar(siteId, csvPath, species, cloudJobId) {
  const { data, error } = await supabase.rpc('harvest_lidar_stems',
    { p_site_id: siteId, p_cloud_job_id: cloudJobId || null })
  if (error) throw new Error(`harvest_lidar_stems failed: ${error.message}`)
  if (!data || !data.length) {
    throw new Error(`no LiDAR trees for site ${siteId} - run the DBH + volume analysis first`)
  }
  const header = ['object_id', 'stem_key', 'dbh_cm', 'stem_volume_m3', 'species', 'lat', 'lon']
  const sp = species || 'Spruce'
  const rows = data
    .filter((tr) => tr.lat != null && tr.lon != null)
    .map((tr) => ({
      object_id: siteId,
      stem_key: tr.tree_id,
      dbh_cm: tr.dbh_cm,
      stem_volume_m3: tr.volume_m3,
      species: sp,
      lat: tr.lat,
      lon: tr.lon,
    }))
  if (!rows.length) throw new Error(`LiDAR trees for site ${siteId} have no coordinates`)
  await writeCsv(csvPath, header, rows)
  return { count: rows.length, cloudJobId: data[0].cloud_job_id || null }
}

// ---- OS Terrain 50 enrichment ---------------------------------------------
// Populate the six _os5 features on each stem so the model runs with terrain
// signal instead of terrain-blind. Reuses extract_terrain.py's validated os5
// sampler (identical to how the model was trained), fetching only the OS tiles
// covering this coupe's bbox from S3. Degrades gracefully: any failure returns
// the un-enriched stems and the quote still runs (just at the wider band).
const OS5_COLS = ['elev_m', 'slope_deg', 'aspect_northness', 'aspect_eastness', 'roughness_m', 'tpi_m']

async function mergeOs5(stemsCsv, terrainCsv, outCsv) {
  const stems = parseCsv(await fsp.readFile(stemsCsv, 'utf8'))
  const terr = parseCsv(await fsp.readFile(terrainCsv, 'utf8'))
  const key = (r) => `${r.object_id}|${r.stem_key}`
  const byKey = new Map(terr.map((r) => [key(r), r]))

  const header = Object.keys(stems[0] || {}).concat(OS5_COLS.map((c) => `${c}_os5`))
  let sampled = 0
  for (const s of stems) {
    const m = byKey.get(key(s))
    for (const c of OS5_COLS) {
      const v = m ? m[c] : ''
      s[`${c}_os5`] = (v === '' || v == null || v === 'nan') ? '' : v
    }
    if (s['slope_deg_os5'] !== '') sampled += 1
  }
  await writeCsv(outCsv, header, stems)
  return stems.length ? sampled / stems.length : 0
}

async function enrichTerrainOS50(work, stemsCsv) {
  try {
    // which OS 10 km tiles cover the coupe?
    const refsOut = await run('python3', [OS_TILES_SCRIPT, '--stems', stemsCsv])
    const refs = refsOut.trim().split(/\s+/).filter(Boolean)
    if (!refs.length) { console.log('[harvest] no coords -> terrain-blind quote'); return { path: stemsCsv, source: 'none', coverage: 0 } }

    // list the terr50 prefix once and keep the tiles whose filename matches a ref
    const listing = await run('aws', ['s3', 'ls', `s3://${S3_BUCKET}/${TERR50_PREFIX}`, '--recursive', '--region', AWS_REGION])
    const keys = listing.split('\n')
      .map((l) => l.trim().split(/\s+/).slice(3).join(' '))
      .filter(Boolean)
    const wanted = keys.filter((k) => {
      const base = (k.split('/').pop() || '').toLowerCase()
      return refs.some((r) => base.startsWith(r))
    })
    if (!wanted.length) { console.log(`[harvest] no OS tiles matched [${refs.join(',')}] -> terrain-blind`); return { path: stemsCsv, source: 'none', coverage: 0 } }

    const tilesDir = path.join(work, 'terr50')
    await fsp.mkdir(tilesDir, { recursive: true })
    for (const k of wanted) {
      await run('aws', ['s3', 'cp', `s3://${S3_BUCKET}/${k}`, path.join(tilesDir, k.split('/').pop()), '--no-progress', '--region', AWS_REGION])
    }
    console.log(`[harvest] fetched ${wanted.length} OS tile(s) for [${refs.join(',')}]`)

    // sample the six _os5 features with the validated extractor, then merge
    const terrainCsv = path.join(work, 'terrain_os5.csv')
    await run('python3', [EXTRACT_TERRAIN, 'os5', '--stems', stemsCsv, '--dtm', tilesDir, '--out', terrainCsv])
    const enriched = path.join(work, 'stems_enriched.csv')
    const coverage = await mergeOs5(stemsCsv, terrainCsv, enriched)
    console.log(`[harvest] OS50 terrain applied to ${(coverage * 100).toFixed(0)}% of stems`)
    return { path: enriched, source: 'os50', coverage }
  } catch (e) {
    console.log('[harvest] terrain enrichment failed, falling back to terrain-blind:', e.message)
    return { path: stemsCsv, source: 'none', coverage: 0 }
  }
}

async function runHarvestQuote(payload) {
  if (!supabase) throw new Error('Supabase env not configured (SUPABASE_URL / SERVICE_ROLE_KEY)')
  const { quoteId, siteId, source = 'coupe', stemsKey, machineRate, fuelPrice } = payload || {}
  if (!quoteId) throw new Error('harvest quote needs { quoteId }')

  // Read the quote row for its inputs (rates may live there rather than the payload).
  const { data: quote, error: qErr } = await supabase
    .from('harvest_quote').select('*').eq('id', quoteId).single()
  if (qErr || !quote) throw new Error(`quote ${quoteId} not found: ${qErr?.message}`)

  const rate = machineRate ?? quote.machine_rate ?? 150
  const fuel = fuelPrice ?? quote.fuel_price ?? 1.30

  await supabase.from('harvest_quote').update({
    status: 'processing', started_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  }).eq('id', quoteId)

  const work = await fsp.mkdtemp(path.join(os.tmpdir(), 'harvest-'))
  let usedCloudJobId = null
  try {
    const stemsCsv = path.join(work, 'stems.csv')
    const outCsv = path.join(work, 'quote.csv')

    // 1) assemble the stems CSV
    if (source === 'upload') {
      if (!stemsKey && !quote.stems_key) throw new Error('upload source needs a stems_key')
      const key = stemsKey || quote.stems_key
      await run('aws', ['s3', 'cp', `s3://${S3_BUCKET}/${key}`, stemsCsv, '--no-progress', '--region', AWS_REGION])
    } else if (source === 'lidar') {
      const sid = siteId || quote.site_id
      if (!sid) throw new Error('lidar source needs a site_id')
      const lr = await stemsFromLidar(sid, stemsCsv, quote.species || payload.species,
        quote.cloud_job_id || payload.cloudJobId)
      usedCloudJobId = lr.cloudJobId
      console.log(`[harvest] pulled ${lr.count} forecast stems from LiDAR cloud ${usedCloudJobId} for site ${sid}`)
    } else {
      const sid = siteId || quote.site_id
      if (!sid) throw new Error('coupe source needs a site_id')
      const n = await stemsFromCoupe(sid, stemsCsv)
      console.log(`[harvest] pulled ${n} stems from harvested_stems for site ${sid}`)
    }

    // 2) enrich with OS Terrain 50 (falls back to raw stems on any failure)
    const enr = await enrichTerrainOS50(work, stemsCsv)

    // 3) run the validated predict tool on the enriched stems
    await run('python3', [
      PREDICT_SCRIPT,
      '--model', MODEL_PATH,
      '--stems', enr.path,
      '--out', outCsv,
      '--machine-rate', String(rate),
      '--fuel-price', String(fuel),
    ])

    // 3) parse outputs
    const summaryRows = parseCsv(await fsp.readFile(outCsv, 'utf8'))
    if (!summaryRows.length) throw new Error('predict_coupe produced an empty summary')
    const agg = aggregateSummary(summaryRows)

    const stemRows = parseCsv(await fsp.readFile(outCsv + '.stems.csv', 'utf8'))

    // 4) persist — summary onto the quote row, per-stem into harvest_quote_stem
    await supabase.from('harvest_quote').update({
      ...agg,
      terrain_source: enr.source,
      os5_coverage: Math.round(enr.coverage * 1000) / 1000,
      cloud_job_id: usedCloudJobId,
      status: 'ready',
      error: null,
      updated_at: new Date().toISOString(),
      completed_at: new Date().toISOString(),
    }).eq('id', quoteId)

    // replace any prior stems for this quote (idempotent re-runs)
    await supabase.from('harvest_quote_stem').delete().eq('quote_id', quoteId)

    const toInsert = stemRows.map((r) => ({
      quote_id: quoteId,
      stem_key: r.stem_key,
      lat: r.lat === '' ? null : Number(r.lat),
      lon: r.lon === '' ? null : Number(r.lon),
      dbh_cm: r.dbh_cm === '' ? null : Number(r.dbh_cm),
      stem_volume_m3: r.stem_volume_m3 === '' ? null : Number(r.stem_volume_m3),
      pred_cycle_s: r.pred_cycle_s === '' ? null : Number(r.pred_cycle_s),
    }))
    const CHUNK = 2000
    for (let i = 0; i < toInsert.length; i += CHUNK) {
      const { error: insErr } = await supabase.from('harvest_quote_stem').insert(toInsert.slice(i, i + CHUNK))
      if (insErr) throw new Error(`stem insert failed at ${i}: ${insErr.message}`)
    }

    console.log(`[harvest] quote ${quoteId} ready: ${agg.volume_m3} m3, ${agg.pmh0} m3/PMH0, `
      + `£${agg.cost_gbp} (${agg.confidence}), ${toInsert.length} stems mapped`)
  } catch (e) {
    await markFailed(quoteId, e)
    throw e
  } finally {
    await fsp.rm(work, { recursive: true, force: true }).catch(() => {})
  }
}

module.exports = { runHarvestQuote, supabase }