// sync.js — Arrol fleet sync. Runs once and exits (AWS Batch job; also submitted
// on demand by the portal's "Sync now"). One process, one pass per vendor:
//
//   Komatsu — list new HPR/MOM/FPR files in the window since the last successful
//   run, download each, archive the raw XML to S3 (machine-files/), register the
//   machine, ledger the file in machine_files, parse HPR into harvested_stems
//   with automatic site + operation matching, and record a fleet_sync_runs row.
//
//   Deere — refresh the stored OAuth connection, list the organisation's
//   equipment, record latest positions into machine_positions and update the
//   machine registry.
//
// Idempotency: machine_files.file_name is UNIQUE — a file is fetched and parsed
// exactly once. Re-parsing (if ever needed) deletes stems by source_file first.
// A vendor failing records its error but does not abort the other vendor.
//
// Env (see fleet-setup.ps1 for how these reach the Batch job definition):
//   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY        (required)
//   S3_BUCKET (default arrol-lidar), AWS_REGION (default eu-west-2)
//   KOMATSU_API_KEY, KOMATSU_API_BASE, KOMATSU_BACKFILL_DAYS (default 14)
//   DEERE_CLIENT_ID, DEERE_CLIENT_SECRET, DEERE_OAUTH_BASE, DEERE_API_BASE
//   SYNC_VENDORS (default "komatsu,deere" — limit for testing, e.g. "komatsu")

'use strict'

const { createClient } = require('@supabase/supabase-js')
const { S3Client, PutObjectCommand } = require('@aws-sdk/client-s3')
const komatsu = require('./komatsu')
const { parseHpr, stemsToHarvestRows } = require('./hpr')
const { loadLeafSites, resolveSite } = require('./sitematch')
const { autoCreateSite } = require('./autosite')
const deere = require('./deere')

const S3_BUCKET = process.env.S3_BUCKET || 'arrol-lidar'
const AWS_REGION = process.env.AWS_REGION || 'eu-west-2'
const BACKFILL_DAYS = parseInt(process.env.KOMATSU_BACKFILL_DAYS || '14', 10)
const VENDORS = (process.env.SYNC_VENDORS || 'komatsu,deere').split(',').map(s => s.trim()).filter(Boolean)
const AUTO_CREATE_SITES = (process.env.FLEET_AUTO_CREATE_SITES || 'true') !== 'false'

function supabaseAdmin() {
  const url = process.env.SUPABASE_URL
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY
  if (!url || !key) throw new Error('SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not configured')
  return createClient(url, key, { auth: { persistSession: false } })
}

// On Fargate the task role supplies credentials; no keys in env needed.
const s3 = new S3Client({ region: AWS_REGION })

async function s3Put(key, body, contentType) {
  await s3.send(new PutObjectCommand({ Bucket: S3_BUCKET, Key: key, Body: body, ContentType: contentType }))
}

// ── Machine registry helpers ──────────────────────────────────────────────────
const machineCache = new Map() // `${vendor}:${vendorMachineId}` -> machines row

async function ensureMachine(supabase, vendor, vendorMachineId, fields) {
  const cacheKey = `${vendor}:${vendorMachineId}`
  if (machineCache.has(cacheKey)) return machineCache.get(cacheKey)

  const { data: existing } = await supabase
    .from('machines').select('*')
    .eq('vendor', vendor).eq('vendor_machine_id', String(vendorMachineId))
    .maybeSingle()
  if (existing) {
    // Upgrade an unknown kind or fill blanks, but never overwrite curated names.
    const patch = {}
    if (existing.kind === 'unknown' && fields.kind && fields.kind !== 'unknown') patch.kind = fields.kind
    if (!existing.name && fields.name) patch.name = fields.name
    if (!existing.make && fields.make) patch.make = fields.make
    if (!existing.model && fields.model) patch.model = fields.model
    if (Object.keys(patch).length) {
      patch.updated_at = new Date().toISOString()
      await supabase.from('machines').update(patch).eq('id', existing.id)
      Object.assign(existing, patch)
    }
    machineCache.set(cacheKey, existing)
    return existing
  }

  const { data: created, error } = await supabase.from('machines').insert({
    vendor,
    vendor_machine_id: String(vendorMachineId),
    name: fields.name || `${vendor === 'komatsu' ? 'Komatsu' : 'Machine'} ${vendorMachineId}`,
    make: fields.make || '',
    model: fields.model || '',
    kind: fields.kind || 'unknown',
  }).select('*').single()
  if (error) throw new Error('machine insert failed: ' + error.message)
  console.log(`[fleet] registered new ${vendor} machine ${vendorMachineId}`)
  machineCache.set(cacheKey, created)
  return created
}

// ── Sync-run bookkeeping ──────────────────────────────────────────────────────
async function startRun(supabase, vendor) {
  const { data, error } = await supabase.from('fleet_sync_runs')
    .insert({ vendor }).select('id,started_at').single()
  if (error) throw new Error('fleet_sync_runs insert failed: ' + error.message)
  return data
}

async function finishRun(supabase, id, patch) {
  await supabase.from('fleet_sync_runs')
    .update({ ...patch, finished_at: new Date().toISOString() }).eq('id', id)
}

async function lastGoodRunStart(supabase, vendor) {
  const { data } = await supabase.from('fleet_sync_runs')
    .select('started_at').eq('vendor', vendor).eq('ok', true)
    .order('started_at', { ascending: false }).limit(1).maybeSingle()
  return data ? new Date(data.started_at) : null
}

// ── Komatsu pass ──────────────────────────────────────────────────────────────
async function runKomatsu(supabase) {
  const run = await startRun(supabase, 'komatsu')
  const counters = { files_found: 0, files_ingested: 0, stems_inserted: 0, machines_seen: 0 }
  const detail = { files: [] }
  try {
    if (!process.env.KOMATSU_API_KEY) throw new Error('KOMATSU_API_KEY not configured')

    // Window: last good run minus a 1h overlap; first run backfills BACKFILL_DAYS.
    const last = await lastGoodRunStart(supabase, 'komatsu')
    const start = last
      ? new Date(last.getTime() - 60 * 60 * 1000)
      : new Date(Date.now() - BACKFILL_DAYS * 24 * 60 * 60 * 1000)
    const end = new Date()
    console.log(`[komatsu] window ${start.toISOString()} -> ${end.toISOString()}`)

    // List all three production file types across all machines on the key.
    const listed = [] // { type, name }
    for (const type of ['hpr', 'mom', 'fpr']) {
      try {
        const names = await komatsu.listFiles(type, start, end)
        for (const name of names) listed.push({ type, name })
        console.log(`[komatsu] ${type.toUpperCase()}: ${names.length} file(s) in window`)
      } catch (e) {
        // A type not enabled on the account should not sink the whole run.
        console.warn(`[komatsu] list ${type} failed: ${e.message}`)
        detail[`list_${type}_error`] = String(e.message).slice(0, 300)
      }
    }
    counters.files_found = listed.length

    // Drop files already in the ledger (file_name UNIQUE = idempotency key).
    const known = new Set()
    for (let i = 0; i < listed.length; i += 200) {
      const batch = listed.slice(i, i + 200).map(f => f.name)
      const { data } = await supabase.from('machine_files').select('file_name').in('file_name', batch)
      for (const r of data || []) known.add(r.file_name)
    }
    const fresh = listed.filter(f => !known.has(f.name))
    console.log(`[komatsu] ${fresh.length} new file(s) to ingest`)

    // Sites loaded once per run for matching.
    const sites = fresh.some(f => f.type === 'hpr') ? await loadLeafSites(supabase) : []

    for (const f of fresh) {
      try {
        const ingested = await ingestKomatsuFile(supabase, f.type, f.name, sites, counters)
        detail.files.push(ingested)
      } catch (e) {
        console.error(`[komatsu] ingest ${f.name} failed:`, e.message)
        detail.files.push({ name: f.name, error: String(e.message).slice(0, 300) })
      }
    }

    // Last-contact heartbeat per known Komatsu machine.
    const { data: kMachines } = await supabase.from('machines')
      .select('id,vendor_machine_id,meta').eq('vendor', 'komatsu')
    counters.machines_seen = (kMachines || []).length
    for (const m of kMachines || []) {
      const lastContact = await komatsu.syncStatus(m.vendor_machine_id)
      await supabase.from('machines').update({
        last_sync_at: new Date().toISOString(),
        meta: { ...(m.meta || {}), last_machine_contact: lastContact },
        updated_at: new Date().toISOString(),
      }).eq('id', m.id)
    }

    await finishRun(supabase, run.id, { ok: true, ...counters, detail })
    console.log(`[komatsu] done: ${counters.files_ingested}/${counters.files_found} files, ${counters.stems_inserted} stems`)
  } catch (e) {
    console.error('[komatsu] run failed:', e.message)
    await finishRun(supabase, run.id, { ok: false, ...counters, error: String(e.message).slice(0, 500), detail })
  }
}

async function ingestKomatsuFile(supabase, type, name, sites, counters) {
  const { chassis, timestamp } = komatsu.parseFileName(name)
  const bytes = await komatsu.getFile(type, name)
  const s3Key = `machine-files/komatsu/${type}/${name}`
  await s3Put(s3Key, bytes, 'application/xml')

  const kindHint = type === 'hpr' ? 'harvester' : type === 'fpr' ? 'forwarder' : 'unknown'
  const machine = chassis
    ? await ensureMachine(supabase, 'komatsu', chassis, { kind: kindHint })
    : null

  const { data: fileRow, error: fileErr } = await supabase.from('machine_files').insert({
    machine_id: machine ? machine.id : null,
    vendor: 'komatsu',
    file_type: type,
    file_name: name,
    s3_key: s3Key,
    size_bytes: bytes.length,
    file_date: timestamp,
  }).select('id').single()
  if (fileErr) {
    // UNIQUE collision = another run beat us to it; treat as already ingested.
    if (String(fileErr.message).includes('duplicate')) return { name, skipped: 'duplicate' }
    throw new Error('machine_files insert failed: ' + fileErr.message)
  }
  counters.files_ingested++

  const result = { name, type, s3Key, size: bytes.length }

  if (type === 'hpr') {
    const parsed = parseHpr(bytes.toString('utf8'), name)
    if (!parsed) {
      await supabase.from('machine_files').update({
        parse_status: 'failed', parse_error: 'HPR parse returned null',
      }).eq('id', fileRow.id)
      result.parse = 'failed'
      return result
    }

    let { siteId, operationId, matchedBy } = await resolveSite(supabase, parsed, sites)

    // No match, but the machine named its harvest object: provision the site
    // from the feed (approximate boundary derived from stem GPS). The new site
    // joins this run's match list, so the object's later files match by name.
    if (!siteId && AUTO_CREATE_SITES && parsed.objectName) {
      const created = await autoCreateSite(supabase, parsed.objectName, parsed.stems,
        'komatsu', parsed.machineId || parsed.machineName)
      if (created) {
        sites.push(created)
        siteId = created.id
        matchedBy = 'auto_created'
      }
    }

    // Replace-then-insert by source_file — same convention as the manual path.
    await supabase.from('harvested_stems').delete().eq('source_file', name)
    const rows = stemsToHarvestRows(parsed, {
      operationId, siteId, machineFileId: fileRow.id, vendor: 'komatsu',
      felledAtIso: timestamp,
    })
    for (let i = 0; i < rows.length; i += 500) {
      const { error } = await supabase.from('harvested_stems').insert(rows.slice(i, i + 500))
      if (error) throw new Error('harvested_stems insert failed: ' + error.message)
    }
    counters.stems_inserted += rows.length

    const summary = {
      stems: parsed.totalStems,
      volume_m3: parsed.totalVolume,
      object_name: parsed.objectName,
      machine_user_id: parsed.machineId,
      matched_by: matchedBy,
    }
    await supabase.from('machine_files').update({
      parse_status: 'parsed', site_id: siteId, operation_id: operationId, summary,
    }).eq('id', fileRow.id)
    Object.assign(result, summary)
  } else {
    // MOM (utilisation) and FPR (forwarded production) are archived now and
    // parsed in the next phase — the raw XML is safe in S3 either way.
    await supabase.from('machine_files').update({ parse_status: 'stored' }).eq('id', fileRow.id)
    result.parse = 'stored'
  }

  if (machine) {
    const newer = !machine.last_file_at || (timestamp && timestamp > machine.last_file_at)
    if (newer) {
      await supabase.from('machines').update({
        last_file_at: timestamp || new Date().toISOString(),
        updated_at: new Date().toISOString(),
      }).eq('id', machine.id)
      machine.last_file_at = timestamp
    }
  }
  return result
}

// ── Deere pass ────────────────────────────────────────────────────────────────
async function runDeere(supabase) {
  const run = await startRun(supabase, 'deere')
  const counters = { machines_seen: 0, positions_recorded: 0 }
  try {
    const valid = await deere.getValidConnection(supabase)
    if (!valid) {
      await finishRun(supabase, run.id, { ok: true, ...counters, detail: { note: 'no deere connection' } })
      console.log('[deere] no connection stored — skipping (connect in the portal first)')
      return
    }
    const { conn, token } = valid
    if (!conn.jd_org_id) {
      await finishRun(supabase, run.id, { ok: true, ...counters, detail: { note: 'connection has no org' } })
      return
    }

    const jdMachines = await deere.listMachines(token, conn.jd_org_id)
    counters.machines_seen = jdMachines.length
    console.log(`[deere] ${jdMachines.length} machine(s) in org ${conn.jd_org_id}`)

    for (const m of jdMachines) {
      const machine = await ensureMachine(supabase, 'deere', m.id, {
        name: m.name || '',
        make: m.equipmentMake || m.make || 'John Deere',
        model: m.equipmentModel || m.model || '',
        kind: deere.classifyKind(m),
      })

      const loc = await deere.latestLocation(token, m)
      const patch = { last_sync_at: new Date().toISOString(), updated_at: new Date().toISOString() }
      if (loc) {
        patch.last_position = loc
        patch.last_position_at = loc.at || new Date().toISOString()
        if (loc.at) {
          const { error } = await supabase.from('machine_positions').upsert({
            machine_id: machine.id,
            latitude: loc.lat,
            longitude: loc.lon,
            recorded_at: loc.at,
            source: 'deere',
          }, { onConflict: 'machine_id,recorded_at', ignoreDuplicates: true })
          if (!error) counters.positions_recorded++
        }
      }
      await supabase.from('machines').update(patch).eq('id', machine.id)
    }

    await finishRun(supabase, run.id, { ok: true, ...counters, detail: {} })
    console.log(`[deere] done: ${counters.positions_recorded} position(s) recorded`)
  } catch (e) {
    console.error('[deere] run failed:', e.message)
    await finishRun(supabase, run.id, { ok: false, ...counters, error: String(e.message).slice(0, 500) })
  }
}

// ── Main ──────────────────────────────────────────────────────────────────────
async function main() {
  console.log(`[fleet] sync starting — vendors: ${VENDORS.join(', ')}`)
  const supabase = supabaseAdmin()
  if (VENDORS.includes('komatsu')) await runKomatsu(supabase)
  if (VENDORS.includes('deere')) await runDeere(supabase)
  console.log('[fleet] sync complete')
}

main().then(
  () => process.exit(0),
  e => { console.error('[fleet] fatal:', e); process.exit(1) },
)
