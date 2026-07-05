// reparse.js — backfill stem taper profiles and per-log detail from the
// HPR XML already archived in S3. No re-download from Komatsu: every parsed
// file's raw XML lives at machine-files/komatsu/hpr/{file_name}.
//
// Pass 1 — for every parsed HPR machine_files row: fetch the XML from S3,
//   re-parse with the extended mapper (taper + log start/butt/top detail),
//   delete-then-insert its stems (same source_file convention as sync), and
//   stamp feed_object_id on the file row. Site/operation assignments are kept
//   from the file row — this never re-matches.
//
// Pass 2 — recompute every feed object's rollups (files/stems/volume/date
//   range/machines/GPS hull) from database truth, filling the hulls the
//   fleet-b.sql migration left null.
//
// Run it once after deploying the Phase B image:
//   AWS Batch job with command override: node,reparse.js
//
// Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, S3_BUCKET, AWS_REGION.
// Optional: REPARSE_LIMIT (cap files for a smoke test, e.g. 5).

'use strict'

const { createClient } = require('@supabase/supabase-js')
const { S3Client, GetObjectCommand } = require('@aws-sdk/client-s3')
const { parseHpr, stemsToHarvestRows } = require('./hpr')
const { normKey, loadFeedObjects, recomputeFeedObject } = require('./feedobjects')

const S3_BUCKET = process.env.S3_BUCKET || 'arrol-lidar'
const AWS_REGION = process.env.AWS_REGION || 'eu-west-2'
const LIMIT = parseInt(process.env.REPARSE_LIMIT || '0', 10) || 0

const s3 = new S3Client({ region: AWS_REGION })

function supabaseAdmin() {
  const url = process.env.SUPABASE_URL
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY
  if (!url || !key) throw new Error('SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not configured')
  return createClient(url, key, { auth: { persistSession: false } })
}

async function s3GetText(key) {
  const res = await s3.send(new GetObjectCommand({ Bucket: S3_BUCKET, Key: key }))
  const bytes = await res.Body.transformToByteArray()
  return Buffer.from(bytes).toString('utf8')
}

async function listParsedHprFiles(supabase) {
  const files = []
  for (let from = 0; ; from += 1000) {
    const { data, error } = await supabase.from('machine_files')
      .select('id,file_name,s3_key,file_date,site_id,operation_id,vendor,summary,feed_object_id')
      .eq('file_type', 'hpr').eq('parse_status', 'parsed')
      .order('fetched_at', { ascending: true })
      .range(from, from + 999)
    if (error) throw new Error('machine_files load failed: ' + error.message)
    files.push(...(data || []))
    if (!data || data.length < 1000) break
  }
  return LIMIT > 0 ? files.slice(0, LIMIT) : files
}

async function main() {
  const supabase = supabaseAdmin()
  const files = await listParsedHprFiles(supabase)
  const feedObjects = await loadFeedObjects(supabase)
  console.log(`[reparse] ${files.length} parsed HPR file(s) to backfill${LIMIT ? ` (REPARSE_LIMIT=${LIMIT})` : ''}`)

  let done = 0
  let stems = 0
  let failed = 0

  for (const f of files) {
    try {
      const xml = await s3GetText(f.s3_key || `machine-files/komatsu/hpr/${f.file_name}`)
      const parsed = parseHpr(xml, f.file_name)
      if (!parsed) {
        failed++
        console.warn(`[reparse] ${f.file_name}: parse returned null — skipped (stems left as they are)`)
        continue
      }

      // Keep the file's existing assignment; only the stem payload is richer.
      const rows = stemsToHarvestRows(parsed, {
        operationId: f.operation_id,
        siteId: f.site_id,
        machineFileId: f.id,
        vendor: f.vendor || 'komatsu',
        felledAtIso: f.file_date,
      })

      await supabase.from('harvested_stems').delete().eq('source_file', f.file_name)
      for (let i = 0; i < rows.length; i += 500) {
        const { error } = await supabase.from('harvested_stems').insert(rows.slice(i, i + 500))
        if (error) throw new Error('harvested_stems insert failed: ' + error.message)
      }
      stems += rows.length

      // Stamp the feed object on the file row if the migration backfill missed it.
      if (!f.feed_object_id) {
        const objName = (f.summary && f.summary.object_name) || parsed.objectName || ''
        const fo = feedObjects.get(normKey(objName))
        if (fo) {
          await supabase.from('machine_files').update({ feed_object_id: fo.id }).eq('id', f.id)
        }
      }

      done++
      if (done % 25 === 0) console.log(`[reparse] ${done}/${files.length} files, ${stems} stems rewritten`)
    } catch (e) {
      failed++
      console.error(`[reparse] ${f.file_name} failed: ${e.message}`)
    }
  }
  console.log(`[reparse] pass 1 complete: ${done} ok, ${failed} failed, ${stems} stems rewritten`)

  console.log('[reparse] pass 2 — recomputing feed object rollups and hulls')
  for (const fo of feedObjects.values()) {
    try {
      const patch = await recomputeFeedObject(supabase, fo)
      console.log(`[reparse] ${fo.object_name}: ${patch.files} files, ${patch.stems} stems, ` +
        `${patch.volume_m3} m3, hull ${patch.hull ? 'derived' : 'none'}`)
    } catch (e) {
      console.error(`[reparse] rollup for ${fo.object_name} failed: ${e.message}`)
    }
  }
  console.log('[reparse] complete')
}

main().then(
  () => process.exit(0),
  e => { console.error('[reparse] fatal:', e); process.exit(1) },
)
