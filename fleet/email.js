// email.js — the StanForD email bridge (Fleet Phase B stage 6b).
//
// TimberManager (John Deere) and other systems can email StanForD files on a
// schedule. SES receives mail for ingest.arrol.cloud and writes each raw
// message to s3://arrol-lidar/machine-files/inbound-email/raw/. Every 'email'
// machine_connections row owns a unique inbox token; its address is
// feed+{token}@ingest.arrol.cloud, so each user (and future org) runs their
// own bridge with full attribution — files are ledgered with the connection's
// id and the connection's import mode governs staging.
//
// Per tick this vendor: lists raw messages, parses the MIME, processes only
// messages addressed to THIS connection's token (others are left for their
// own connection's run), ingests .hpr attachments through the shared feed
// pipeline (mapping rules -> route or stage) and archives .mom files, then
// moves the raw message to processed/. machine_files.file_name UNIQUE dedupes
// across email, manual upload, and any future source.
//
// IAM note: the task role needs s3:DeleteObject on machine-files/* for the
// raw -> processed move (added in the stage 6b setup steps).

'use strict'

const {
  S3Client, ListObjectsV2Command, GetObjectCommand, PutObjectCommand,
  CopyObjectCommand, DeleteObjectCommand,
} = require('@aws-sdk/client-s3')
const { simpleParser } = require('mailparser')
const { parseHpr, stemsToHarvestRows } = require('./hpr')
const { loadFeedObjects, ensureFeedObject, applyFileRollups, normKey } = require('./feedobjects')

const S3_BUCKET = process.env.S3_BUCKET || 'arrol-lidar'
const AWS_REGION = process.env.AWS_REGION || 'eu-west-2'
const RAW_PREFIX = 'machine-files/inbound-email/raw/'
const PROCESSED_PREFIX = 'machine-files/inbound-email/processed/'

const s3 = new S3Client({ region: AWS_REGION })

async function s3Bytes(key) {
  const res = await s3.send(new GetObjectCommand({ Bucket: S3_BUCKET, Key: key }))
  return Buffer.from(await res.Body.transformToByteArray())
}

async function listRaw() {
  const keys = []
  let token
  do {
    const res = await s3.send(new ListObjectsV2Command({
      Bucket: S3_BUCKET, Prefix: RAW_PREFIX, ContinuationToken: token,
    }))
    for (const o of res.Contents || []) {
      if (o.Key && o.Key !== RAW_PREFIX) keys.push(o.Key)
    }
    token = res.IsTruncated ? res.NextContinuationToken : undefined
  } while (token)
  return keys
}

function recipientAddresses(mail) {
  const out = new Set()
  for (const field of [mail.to, mail.cc, mail.bcc]) {
    for (const v of (field && field.value) || []) {
      if (v.address) out.add(String(v.address).toLowerCase())
    }
  }
  // SES adds the actual envelope recipient headers on delivery.
  for (const h of ['x-original-to', 'delivered-to']) {
    const val = mail.headers && mail.headers.get(h)
    if (val) out.add(String(val).toLowerCase())
  }
  return Array.from(out)
}

const sanitise = name => String(name || '').replace(/[^\w.\-]+/g, '_')

// ── Ingest one HPR attachment through the shared feed pipeline ────────────────
async function ingestHpr(supabase, conn, importMode, name, bytes, feedObjects, counters) {
  const parsed = parseHpr(bytes.toString('utf8'), name)
  if (!parsed) return { name, error: 'not a readable StanForD 2010 HPR' }

  const s3Key = `machine-files/email/hpr/${name}`
  await s3.send(new PutObjectCommand({
    Bucket: S3_BUCKET, Key: s3Key, Body: bytes, ContentType: 'application/xml',
  }))

  const fileDate = parsed.endDate || parsed.startDate
    ? `${parsed.endDate || parsed.startDate}T12:00:00Z`
    : new Date().toISOString()

  const { data: fileRow, error: fileErr } = await supabase.from('machine_files').insert({
    machine_id: null,
    vendor: 'email',
    file_type: 'hpr',
    file_name: name,
    s3_key: s3Key,
    size_bytes: bytes.length,
    file_date: fileDate,
    connection_id: conn.id,
  }).select('id').single()
  if (fileErr) {
    if (String(fileErr.message).includes('duplicate')) return { name, skipped: 'duplicate' }
    throw new Error('machine_files insert failed: ' + fileErr.message)
  }
  counters.files_ingested++

  // Mapping rules — identical routing to the Komatsu pass. The email bridge
  // never auto-creates sites regardless of mode: unknown objects always stage.
  const objectKey = normKey(parsed.objectName)
  let feedObj = objectKey ? feedObjects.get(objectKey) || null : null
  let siteId = null
  let operationId = null
  let matchedBy = 'no_object_name'
  if (objectKey) {
    if (feedObj && feedObj.status === 'linked') {
      siteId = feedObj.site_id
      operationId = feedObj.operation_id
      matchedBy = 'mapping_rule'
    } else if (feedObj && feedObj.status === 'ignored') {
      matchedBy = 'ignored'
    } else {
      matchedBy = 'staged'
      feedObj = await ensureFeedObject(supabase, feedObjects, parsed.objectName, 'email', null)
    }
  }

  await supabase.from('harvested_stems').delete().eq('source_file', name)
  const rows = stemsToHarvestRows(parsed, {
    operationId, siteId, machineFileId: fileRow.id, vendor: 'email', felledAtIso: fileDate,
  })
  for (let i = 0; i < rows.length; i += 500) {
    const { error } = await supabase.from('harvested_stems').insert(rows.slice(i, i + 500))
    if (error) throw new Error('harvested_stems insert failed: ' + error.message)
  }
  counters.stems_inserted += rows.length

  await supabase.from('machine_files').update({
    parse_status: 'parsed',
    site_id: siteId,
    operation_id: operationId,
    feed_object_id: feedObj ? feedObj.id : null,
    summary: {
      stems: parsed.totalStems,
      volume_m3: parsed.totalVolume,
      object_name: parsed.objectName,
      machine_user_id: parsed.machineId,
      matched_by: matchedBy,
    },
  }).eq('id', fileRow.id)

  if (feedObj) {
    await applyFileRollups(supabase, feedObj, parsed, parsed.machineId || parsed.machineName, fileDate)
  }
  return { name, ok: true, object: parsed.objectName, stems: parsed.totalStems, matched_by: matchedBy }
}

// MOM (and other StanForD types) are archived for later phases.
async function storeAttachment(supabase, conn, type, name, bytes, mailDateIso, counters) {
  const s3Key = `machine-files/email/${type}/${name}`
  await s3.send(new PutObjectCommand({
    Bucket: S3_BUCKET, Key: s3Key, Body: bytes, ContentType: 'application/xml',
  }))
  const { error } = await supabase.from('machine_files').insert({
    machine_id: null,
    vendor: 'email',
    file_type: type,
    file_name: name,
    s3_key: s3Key,
    size_bytes: bytes.length,
    file_date: mailDateIso,
    connection_id: conn.id,
    parse_status: 'stored',
  })
  if (error) {
    if (String(error.message).includes('duplicate')) return { name, skipped: 'duplicate' }
    throw new Error('machine_files insert failed: ' + error.message)
  }
  counters.files_ingested++
  return { name, ok: true, stored: type }
}

// ── The per-connection pass ───────────────────────────────────────────────────
async function runEmail(supabase, conn, startRun, finishRun) {
  const run = await startRun(supabase, 'email', conn.id)
  const counters = { files_found: 0, files_ingested: 0, stems_inserted: 0, machines_seen: 0 }
  const detail = { messages: [] }
  const token = String((conn.options || {}).inbox_token || '').toLowerCase()
  const importMode = (conn.options || {}).import_mode === 'auto' ? 'auto' : 'staged'
  const wantMom = ((conn.options || {}).file_types || ['hpr', 'mom']).includes('mom')

  try {
    if (!token) throw new Error('connection has no inbox_token — recreate it from settings')
    const marker = `feed+${token}@`
    const keys = await listRaw()
    console.log(`[email] "${conn.label}" — ${keys.length} raw message(s) in the inbox prefix`)

    const feedObjects = await loadFeedObjects(supabase)

    for (const key of keys) {
      let mail
      try {
        mail = await simpleParser(await s3Bytes(key))
      } catch (e) {
        console.warn(`[email] unreadable message ${key}: ${e.message} — leaving in place`)
        continue
      }

      const rcpts = recipientAddresses(mail)
      if (!rcpts.some(a => a.startsWith(marker))) continue // another connection's mail

      const msg = { key, from: (mail.from && mail.from.text) || '', files: [] }
      const attachments = mail.attachments || []
      for (const att of attachments) {
        const name = sanitise(att.filename)
        if (!name || !att.content) continue
        try {
          if (/\.hpr$/i.test(name)) {
            counters.files_found++
            msg.files.push(await ingestHpr(supabase, conn, importMode, name, att.content, feedObjects, counters))
          } else if (wantMom && /\.mom$/i.test(name)) {
            counters.files_found++
            const when = mail.date ? new Date(mail.date).toISOString() : new Date().toISOString()
            msg.files.push(await storeAttachment(supabase, conn, 'mom', name, att.content, when, counters))
          }
        } catch (e) {
          console.error(`[email] ${name} failed: ${e.message}`)
          msg.files.push({ name, error: String(e.message).slice(0, 200) })
        }
      }

      // Archive the raw message whether or not it carried usable attachments —
      // it was addressed to this connection and has been dealt with.
      const dest = PROCESSED_PREFIX + key.slice(RAW_PREFIX.length)
      try {
        await s3.send(new CopyObjectCommand({
          Bucket: S3_BUCKET, Key: dest, CopySource: `${S3_BUCKET}/${encodeURIComponent(key)}`,
        }))
        await s3.send(new DeleteObjectCommand({ Bucket: S3_BUCKET, Key: key }))
      } catch (e) {
        console.warn(`[email] could not archive ${key}: ${e.message} (message may be reprocessed; dedupe protects the data)`)
      }
      detail.messages.push(msg)
      console.log(`[email] processed message from ${msg.from}: ${msg.files.length} StanForD file(s)`)
    }

    await finishRun(supabase, run.id, { ok: true, ...counters, detail })
    console.log(`[email] done: ${counters.files_ingested}/${counters.files_found} files, ${counters.stems_inserted} stems`)
    return { ok: true }
  } catch (e) {
    console.error('[email] run failed:', e.message)
    await finishRun(supabase, run.id, { ok: false, ...counters, error: String(e.message).slice(0, 500), detail })
    return { ok: false, error: e.message }
  }
}

module.exports = { runEmail }
