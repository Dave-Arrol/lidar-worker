// rematch.js — re-run site matching for already-ingested HPR files.
//
// Everything needed is already in the database: the harvest object name lives in
// machine_files.summary and the stem GPS in harvested_stems — no re-download.
// For each parsed HPR with no site, it retries the match (name, then GPS) and,
// when the file names its harvest object, auto-creates the site exactly as the
// live sync now does. machine_files and harvested_stems are updated together.
//
// Run as a one-off Batch job with a command override:
//   aws batch submit-job --job-name fleet-rematch --job-queue arrol-fleet-queue \
//     --job-definition arrol-fleet-sync --region eu-west-2 \
//     --container-overrides command=node,rematch.js
//
// Idempotent and safe to re-run: files that gain a site are skipped next time.

'use strict'

const { createClient } = require('@supabase/supabase-js')
const { loadLeafSites, matchByName, matchByGps } = require('./sitematch')
const { autoCreateSite } = require('./autosite')

const AUTO_CREATE_SITES = (process.env.FLEET_AUTO_CREATE_SITES || 'true') !== 'false'

function supabaseAdmin() {
  const url = process.env.SUPABASE_URL
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY
  if (!url || !key) throw new Error('SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not configured')
  return createClient(url, key, { auth: { persistSession: false } })
}

async function linkOperation(supabase, siteId) {
  const { data: ops } = await supabase.from('site_operations')
    .select('id,status').eq('site_id', siteId).eq('type', 'harvesting')
  const list = ops || []
  const inProgress = list.filter(o => o.status === 'in_progress')
  if (inProgress.length === 1) return inProgress[0].id
  if (inProgress.length === 0 && list.length === 1) return list[0].id
  return null
}

async function main() {
  const supabase = supabaseAdmin()
  const sites = await loadLeafSites(supabase)
  console.log(`[rematch] ${sites.length} candidate site(s) loaded`)

  const { data: files, error } = await supabase.from('machine_files')
    .select('id,file_name,vendor,summary,machine_id')
    .eq('file_type', 'hpr').eq('parse_status', 'parsed').is('site_id', null)
    .order('file_date', { ascending: true })
  if (error) throw new Error('machine_files load failed: ' + error.message)
  console.log(`[rematch] ${files.length} unassigned HPR file(s) to process`)

  let matched = 0, created = 0, unresolved = 0
  for (const f of files) {
    const objectName = (f.summary && f.summary.object_name) || ''

    // Stem GPS from what we already ingested.
    const { data: stems } = await supabase.from('harvested_stems')
      .select('latitude,longitude').eq('source_file', f.file_name).limit(2000)
    const pts = (stems || [])
      .filter(s => s.latitude != null && s.longitude != null)
      .map(s => ({ lat: s.latitude, lon: s.longitude }))

    let site = matchByName(objectName, sites)
    let matchedBy = site ? 'object_name' : null
    if (!site && pts.length) {
      site = matchByGps(pts, sites)
      if (site) matchedBy = 'gps'
    }
    if (!site && AUTO_CREATE_SITES && objectName) {
      site = await autoCreateSite(supabase, objectName, pts, f.vendor, f.file_name.split('_')[0])
      if (site) { sites.push(site); matchedBy = 'auto_created'; created++ }
    }
    if (!site) { unresolved++; continue }

    const operationId = await linkOperation(supabase, site.id)
    await supabase.from('machine_files').update({
      site_id: site.id,
      operation_id: operationId,
      summary: { ...(f.summary || {}), matched_by: matchedBy },
    }).eq('id', f.id)
    await supabase.from('harvested_stems').update({
      site_id: site.id, operation_id: operationId,
    }).eq('source_file', f.file_name)

    matched++
    console.log(`[rematch] ${f.file_name} -> "${site.name}" (${matchedBy})`)
  }

  console.log(`[rematch] done: ${matched} assigned (${created} sites created), ${unresolved} unresolved`)
}

main().then(
  () => process.exit(0),
  e => { console.error('[rematch] fatal:', e); process.exit(1) },
)
