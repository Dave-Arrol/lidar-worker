// feedobjects.js — the staged import inbox (feed_objects table).
//
// One row per harvest object seen in the feed. A 'linked' row IS the persisted
// mapping rule: sync.js routes that object's files straight to its site and
// operation. 'ignored' rows keep ingesting silently with no site. 'pending'
// rows accumulate rollups (files / stems / volume / GPS hull / machines) and
// wait in the portal inbox for the user to link, create, or ignore.
//
// The hull stored here is the PURE convex hull of stem positions (unbuffered);
// buffering happens only when a site boundary is derived from it, so repeated
// merges never inflate the shape.

'use strict'

const { hullFromLatLon } = require('./autosite')

/** Normalised object key — must match the SQL seed in fleet-b.sql:
 *  btrim(regexp_replace(lower(x), '[^a-z0-9]+', ' ', 'g')) */
function normKey(s) {
  return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim()
}

/** Load every feed object into a Map keyed by object_key (small table). */
async function loadFeedObjects(supabase) {
  const { data, error } = await supabase.from('feed_objects').select('*')
  if (error) throw new Error('feed_objects load failed: ' + error.message)
  const map = new Map()
  for (const row of data || []) map.set(row.object_key, row)
  return map
}

/**
 * Get or create the feed object for a harvest object name. New objects start
 * 'pending' (staged) unless fields override that. Handles the duplicate-insert
 * race by refetching. Returns the row (also cached into the map).
 */
async function ensureFeedObject(supabase, map, objectName, vendor, fields) {
  const key = normKey(objectName)
  if (!key) return null
  if (map.has(key)) return map.get(key)

  const insert = {
    object_key: key,
    object_name: String(objectName).trim(),
    vendor: vendor || '',
    status: 'pending',
    origin: 'feed',
    ...(fields || {}),
  }
  const { data, error } = await supabase.from('feed_objects')
    .insert(insert).select('*').single()
  if (error) {
    if (String(error.message).includes('duplicate')) {
      const { data: existing } = await supabase.from('feed_objects')
        .select('*').eq('object_key', key).maybeSingle()
      if (existing) { map.set(key, existing); return existing }
    }
    throw new Error('feed_objects insert failed: ' + error.message)
  }
  console.log(`[feed] new harvest object "${insert.object_name}" -> ${insert.status}`)
  map.set(key, data)
  return data
}

/**
 * Fold one parsed HPR file into a feed object's rollups: counters, date range,
 * machines seen, and the merged GPS hull (hull of old hull vertices + new stem
 * points — exact for convex hulls, so no drift over hundreds of files).
 */
async function applyFileRollups(supabase, feedObj, parsed, machineLabel, fileIso) {
  const pts = parsed.stems
    .filter(s => s.lat != null && s.lon != null)
    .map(s => ({ lat: s.lat, lon: s.lon }))
  if (feedObj.hull && feedObj.hull.coordinates && feedObj.hull.coordinates[0]) {
    for (const c of feedObj.hull.coordinates[0]) pts.push({ lon: c[0], lat: c[1] })
  }
  const hull = hullFromLatLon(pts) || feedObj.hull || null

  const machines = Array.isArray(feedObj.machines) ? feedObj.machines.slice() : []
  if (machineLabel && !machines.includes(String(machineLabel))) machines.push(String(machineLabel))

  const t = fileIso ? new Date(fileIso) : null
  const first = feedObj.first_file_at ? new Date(feedObj.first_file_at) : null
  const last = feedObj.last_file_at ? new Date(feedObj.last_file_at) : null

  const patch = {
    files: (feedObj.files || 0) + 1,
    stems: (feedObj.stems || 0) + (parsed.totalStems || 0),
    volume_m3: (feedObj.volume_m3 || 0) + (parsed.totalVolume || 0),
    first_file_at: t && (!first || t < first) ? t.toISOString() : feedObj.first_file_at,
    last_file_at: t && (!last || t > last) ? t.toISOString() : feedObj.last_file_at,
    machines,
    hull,
    updated_at: new Date().toISOString(),
  }
  const { error } = await supabase.from('feed_objects').update(patch).eq('id', feedObj.id)
  if (error) console.warn(`[feed] rollup update failed for ${feedObj.object_key}: ${error.message}`)
  else Object.assign(feedObj, patch)
}

/**
 * Recompute a feed object's rollups from database truth (used by reparse.js
 * after a backfill, and available to the portal after re-linking). Walks the
 * object's machine_files and their stems with paging — Supabase caps selects
 * at 1000 rows, and Carter Thinnings alone has 3,703 stems.
 */
async function recomputeFeedObject(supabase, feedObj) {
  const files = []
  for (let from = 0; ; from += 1000) {
    const { data, error } = await supabase.from('machine_files')
      .select('id,file_date')
      .eq('feed_object_id', feedObj.id)
      .range(from, from + 999)
    if (error) throw new Error('machine_files page failed: ' + error.message)
    files.push(...(data || []))
    if (!data || data.length < 1000) break
  }

  let stems = 0
  let volume = 0
  const pts = []
  const machines = new Set()
  const ids = files.map(f => f.id)
  for (let i = 0; i < ids.length; i += 100) {
    const batch = ids.slice(i, i + 100)
    for (let from = 0; ; from += 1000) {
      const { data, error } = await supabase.from('harvested_stems')
        .select('latitude,longitude,volume_ob_m3,machine')
        .in('machine_file_id', batch)
        .range(from, from + 999)
      if (error) throw new Error('harvested_stems page failed: ' + error.message)
      for (const s of data || []) {
        stems++
        volume += s.volume_ob_m3 || 0
        if (s.latitude != null && s.longitude != null) pts.push({ lat: s.latitude, lon: s.longitude })
        if (s.machine) machines.add(s.machine)
      }
      if (!data || data.length < 1000) break
    }
  }

  const dates = files.map(f => f.file_date).filter(Boolean).sort()
  const patch = {
    files: files.length,
    stems,
    volume_m3: Math.round(volume * 1000) / 1000,
    first_file_at: dates[0] || feedObj.first_file_at,
    last_file_at: dates[dates.length - 1] || feedObj.last_file_at,
    machines: Array.from(machines),
    hull: hullFromLatLon(pts) || feedObj.hull || null,
    updated_at: new Date().toISOString(),
  }
  const { error } = await supabase.from('feed_objects').update(patch).eq('id', feedObj.id)
  if (error) throw new Error('feed_objects recompute update failed: ' + error.message)
  Object.assign(feedObj, patch)
  return patch
}

module.exports = { normKey, loadFeedObjects, ensureFeedObject, applyFileRollups, recomputeFeedObject }
