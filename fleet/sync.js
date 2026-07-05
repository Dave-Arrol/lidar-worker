// sync.js — Arrol fleet sync (Phase B: connection-driven, staged import).
//
// One AWS Batch job definition, two modes, routed by env:
//
//   CONNECTION_ID set   -> sync that one machine_connections row. Credentials
//                          are resolved from Secrets Manager at runtime; the
//                          row's options control file types, backfill window
//                          and import mode.
//
//   CONNECTION_ID unset -> dispatcher tick (dispatch.js): submit one
//                          per-connection job for every active connection whose
//                          next_run_at has arrived. This is what the 15-minute
//                          EventBridge schedule runs, so the schedule itself
//                          never changes — pause and frequency live in the DB.
//
// Import modes (connection options.import_mode):
//   staged (default) — files are archived, ledgered and parsed immediately,
//     but site routing comes ONLY from feed_objects mapping rules. Objects
//     with a 'linked' rule flow straight to their site + operation; 'ignored'
//     objects ingest silently unassigned; anything else lands 'pending' in the
//     portal inbox with live rollups (stems inserted with site_id null so the
//     drill-down works before the user has placed the object).
//   auto — the Phase A behaviour: name/GPS site matching, then auto site
//     creation; the result is recorded as a 'linked' feed object so it also
//     becomes a mapping rule.
//
// Idempotency: machine_files.file_name is UNIQUE — a file is fetched and
// parsed exactly once. Re-parsing deletes stems by source_file first.
//
// Env (see the Phase B setup steps):
//   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY        (required)
//   S3_BUCKET (default arrol-lidar), AWS_REGION (default eu-west-2)
//   CONNECTION_ID (per-connection jobs, set by the dispatcher)
//   FLEET_FORCE=1 (dispatcher mode: treat all active connections as due)
//   KOMATSU_API_BASE, DEERE_* (bases + Deere app credentials, from job def)

'use strict'

const { createClient } = require('@supabase/supabase-js')
const { S3Client, PutObjectCommand } = require('@aws-sdk/client-s3')
const komatsu = require('./komatsu')
const { parseHpr, stemsToHarvestRows } = require('./hpr')
const { loadLeafSites, resolveSite } = require('./sitematch')
const { autoCreateSite } = require('./autosite')
const deere = require('./deere')
const { loadConnections, resolveCredential, recordRunResult } = require('./connections')
const { runEmail } = require('./email')
const { loadFeedObjects, ensureFeedObject, applyFileRollups, normKey } = require('./feedobjects')
const { runDispatch } = require('./dispatch')

const S3_BUCKET = process.env.S3_BUCKET || 'arrol-lidar'
const AWS_REGION = process.env.AWS_REGION || 'eu-west-2'

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
async function startRun(supabase, vendor, connectionId) {
  const { data, error } = await supabase.from('fleet_sync_runs')
    .insert({ vendor, connection_id: connectionId || null }).select('id,started_at').single()
  if (error) throw new Error('fleet_sync_runs insert failed: ' + error.message)
  return data
}

async function finishRun(supabase, id, patch) {
  await supabase.from('fleet_sync_runs')
    .update({ ...patch, finished_at: new Date().toISOString() }).eq('id', id)
}

// Window anchor: this connection's last good run, falling back to the vendor's
// (pre-Phase-B runs have no connection_id), so the migration never re-lists
// the whole backfill window.
async function lastGoodRunStart(supabase, vendor, connectionId) {
  if (connectionId) {
    const { data } = await supabase.from('fleet_sync_runs')
      .select('started_at').eq('connection_id', connectionId).eq('ok', true)
      .order('started_at', { ascending: false }).limit(1).maybeSingle()
    if (data) return new Date(data.started_at)
  }
  const { data } = await supabase.from('fleet_sync_runs')
    .select('started_at').eq('vendor', vendor).eq('ok', true)
    .order('started_at', { ascending: false }).limit(1).maybeSingle()
  return data ? new Date(data.started_at) : null
}

// ── Komatsu pass ──────────────────────────────────────────────────────────────
async function runKomatsu(supabase, conn) {
  const run = await startRun(supabase, 'komatsu', conn.id)
  const counters = { files_found: 0, files_ingested: 0, stems_inserted: 0, machines_seen: 0 }
  const detail = { files: [] }
  const options = conn.options || {}
  const fileTypes = Array.isArray(options.file_types) && options.file_types.length
    ? options.file_types.map(t => String(t).toLowerCase())
    : ['hpr', 'mom', 'fpr']
  const backfillDays = parseInt(options.backfill_days, 10) || 14
  const importMode = options.import_mode === 'auto' ? 'auto' : 'staged'

  try {
    // Window: last good run minus a 1h overlap; first run backfills backfillDays.
    const last = await lastGoodRunStart(supabase, 'komatsu', conn.id)
    const start = last
      ? new Date(last.getTime() - 60 * 60 * 1000)
      : new Date(Date.now() - backfillDays * 24 * 60 * 60 * 1000)
    const end = new Date()
    console.log(`[komatsu] "${conn.label}" mode=${importMode} window ${start.toISOString()} -> ${end.toISOString()}`)

    // List the connection's enabled file types across all machines on the key.
    const listed = [] // { type, name }
    for (const type of fileTypes) {
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

    const hasHpr = fresh.some(f => f.type === 'hpr')
    // Mapping rules always load; leaf sites only matter in auto mode.
    const feedObjects = hasHpr ? await loadFeedObjects(supabase) : new Map()
    const sites = hasHpr && importMode === 'auto' ? await loadLeafSites(supabase) : []

    for (const f of fresh) {
      try {
        const ingested = await ingestKomatsuFile(supabase, conn, importMode, f.type, f.name,
          feedObjects, sites, counters)
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
    return { ok: true }
  } catch (e) {
    console.error('[komatsu] run failed:', e.message)
    await finishRun(supabase, run.id, { ok: false, ...counters, error: String(e.message).slice(0, 500), detail })
    return { ok: false, error: e.message }
  }
}

async function ingestKomatsuFile(supabase, conn, importMode, type, name, feedObjects, sites, counters) {
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
    connection_id: conn.id,
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

    // ── Site routing ─────────────────────────────────────────────────────────
    // Mapping rules first, in every mode. Then, only in auto mode, the Phase A
    // name/GPS matching and auto site creation. Staged mode never guesses: an
    // unmapped object lands 'pending' in the inbox and its stems stay
    // unassigned until the user decides.
    let siteId = null
    let operationId = null
    let matchedBy = ''
    let feedObj = null
    const objectKey = normKey(parsed.objectName)

    if (!objectKey) {
      matchedBy = 'no_object_name'
    } else {
      feedObj = feedObjects.get(objectKey) || null
      if (feedObj && feedObj.status === 'linked') {
        siteId = feedObj.site_id
        operationId = feedObj.operation_id
        matchedBy = 'mapping_rule'
      } else if (feedObj && feedObj.status === 'ignored') {
        matchedBy = 'ignored'
      } else if (importMode === 'auto') {
        ;({ siteId, operationId, matchedBy } = await resolveSite(supabase, parsed, sites))
        if (!siteId && parsed.objectName) {
          const created = await autoCreateSite(supabase, parsed.objectName, parsed.stems,
            'komatsu', parsed.machineId || parsed.machineName)
          if (created) {
            sites.push(created)
            siteId = created.id
            matchedBy = 'auto_created'
          }
        }
        // Record the outcome as a rule so future files route without matching.
        feedObj = await ensureFeedObject(supabase, feedObjects, parsed.objectName, 'komatsu', {
          status: siteId ? 'linked' : 'pending',
          site_id: siteId,
          operation_id: operationId || null,
        })
        // The object may pre-exist as 'pending'; a fresh auto match links it.
        if (feedObj && siteId && feedObj.status !== 'linked') {
          await supabase.from('feed_objects').update({
            status: 'linked', site_id: siteId, operation_id: operationId || null,
            decided_at: new Date().toISOString(), decided_by: 'auto',
            updated_at: new Date().toISOString(),
          }).eq('id', feedObj.id)
          Object.assign(feedObj, { status: 'linked', site_id: siteId, operation_id: operationId || null })
        }
      } else {
        matchedBy = 'staged'
        feedObj = await ensureFeedObject(supabase, feedObjects, parsed.objectName, 'komatsu', null)
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
      feed_object_id: feedObj ? feedObj.id : null,
    }).eq('id', fileRow.id)
    Object.assign(result, summary)

    // Live inbox rollups: counters, date range, machines, merged GPS hull.
    if (feedObj) {
      await applyFileRollups(supabase, feedObj, parsed, chassis || parsed.machineId, timestamp)
    }
  } else {
    // MOM (utilisation) and FPR (forwarded production) are archived now and
    // parsed in a later phase — the raw XML is safe in S3 either way.
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
async function runDeere(supabase, conn) {
  const run = await startRun(supabase, 'deere', conn.id)
  const counters = { machines_seen: 0, positions_recorded: 0 }
  try {
    const valid = await deere.getValidConnection(supabase)
    if (!valid) {
      await finishRun(supabase, run.id, { ok: true, ...counters, detail: { note: 'no deere connection' } })
      console.log('[deere] no OAuth connection stored — skipping (connect in the portal first)')
      return { ok: true }
    }
    const { conn: oauth, token } = valid
    if (!oauth.jd_org_id) {
      await finishRun(supabase, run.id, { ok: true, ...counters, detail: { note: 'connection has no org' } })
      return { ok: true }
    }

    const jdMachines = await deere.listMachines(token, oauth.jd_org_id)
    counters.machines_seen = jdMachines.length
    console.log(`[deere] ${jdMachines.length} machine(s) in org ${oauth.jd_org_id}`)

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
    return { ok: true }
  } catch (e) {
    console.error('[deere] run failed:', e.message)
    await finishRun(supabase, run.id, { ok: false, ...counters, error: String(e.message).slice(0, 500) })
    return { ok: false, error: e.message }
  }
}

// ── Per-connection run ────────────────────────────────────────────────────────
async function runConnection(supabase, conn) {
  console.log(`[fleet] connection ${conn.id} — ${conn.vendor} "${conn.label}" (${conn.status})`)
  let outcome = { ok: false, error: 'unknown vendor' }
  try {
    if (conn.vendor === 'komatsu') {
      const key = await resolveCredential(conn)
      if (!key) throw new Error('no credential resolved for komatsu connection')
      process.env.KOMATSU_API_KEY = key
      outcome = await runKomatsu(supabase, conn)
    } else if (conn.vendor === 'deere') {
      // Deere app credentials arrive via the job definition env; the OAuth
      // token lives in deere_connections. The row here governs scheduling.
      outcome = await runDeere(supabase, conn)
    } else if (conn.vendor === 'email') {
      // The StanForD email bridge — no credentials; the connection's inbox
      // token identifies its mail in the SES drop prefix.
      outcome = await runEmail(supabase, conn, startRun, finishRun)
    }
  } catch (e) {
    outcome = { ok: false, error: e.message }
    console.error(`[fleet] connection ${conn.id} failed:`, e.message)
  }
  await recordRunResult(supabase, conn.id, outcome.ok, outcome.error)
  return outcome
}

// ── Main ──────────────────────────────────────────────────────────────────────
async function main() {
  const supabase = supabaseAdmin()
  const connectionId = process.env.CONNECTION_ID

  if (connectionId) {
    // Targeted run (paused rows included — explicit targeting is deliberate,
    // e.g. testing the Deere connection while it stays paused for the tick).
    const rows = await loadConnections(supabase, { id: connectionId })
    if (!rows.length) throw new Error(`connection ${connectionId} not found`)
    await runConnection(supabase, rows[0])
    console.log('[fleet] sync complete')
    return
  }

  console.log('[fleet] no CONNECTION_ID — running dispatcher tick')
  await runDispatch(supabase)
}

main().then(
  () => process.exit(0),
  e => { console.error('[fleet] fatal:', e); process.exit(1) },
)
