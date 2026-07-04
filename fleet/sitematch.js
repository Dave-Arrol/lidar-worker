// sitematch.js — resolve a production file to an Arrol site.
//
// Two signals, strongest first:
//   1) ObjectName — the harvest object name the operator keyed into the machine,
//      matched against site name / FLS reference (normalised equality, or the
//      full site name contained inside the object name).
//   2) GPS — the majority of located stems falling inside a site boundary
//      (ray-cast point-in-polygon on the sites.boundary GeoJSON).
//
// Files that match neither stay unassigned (site_id null) and surface in the
// portal for manual assignment — never guess.

'use strict'

function norm(s) {
  return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim()
}

/** Leaf sites (not portfolios/estates) with what matching needs. */
async function loadLeafSites(supabase) {
  const { data, error } = await supabase
    .from('sites')
    .select('id,name,fls_reference,kind,boundary,parent_id')
    .is('deleted_at', null)
  if (error) throw new Error('sites load failed: ' + error.message)
  return (data || []).filter(s => s.kind !== 'portfolio' && s.kind !== 'estate')
}

/** 1) ObjectName match. Returns a site or null. */
function matchByName(objectName, sites) {
  const obj = norm(objectName)
  if (!obj) return null
  for (const s of sites) {
    const name = norm(s.name)
    const fls = norm(s.fls_reference)
    if (name && name === obj) return s
    if (fls && fls === obj) return s
  }
  // Containment: the operator often keys "Glen Ample Cpt 12" for site "Glen Ample".
  // Require a reasonably long site name to avoid one-word false positives.
  for (const s of sites) {
    const name = norm(s.name)
    if (name.length >= 5 && obj.includes(name)) return s
  }
  return null
}

// ── Ray-cast point-in-polygon over GeoJSON ────────────────────────────────────
function inRing(lon, lat, ring) {
  let inside = false
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i][0], yi = ring[i][1]
    const xj = ring[j][0], yj = ring[j][1]
    const intersect = ((yi > lat) !== (yj > lat)) &&
      (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi)
    if (intersect) inside = !inside
  }
  return inside
}

function inPolygon(lon, lat, coords) {
  // coords = [outerRing, hole, hole...]
  if (!coords || !coords.length) return false
  if (!inRing(lon, lat, coords[0])) return false
  for (let i = 1; i < coords.length; i++) {
    if (inRing(lon, lat, coords[i])) return false
  }
  return true
}

/** True if (lon,lat) lies inside a GeoJSON geometry / Feature / FeatureCollection. */
function pointInBoundary(lon, lat, boundary) {
  if (!boundary || typeof boundary !== 'object') return false
  const t = boundary.type
  if (t === 'FeatureCollection') {
    return (boundary.features || []).some(f => pointInBoundary(lon, lat, f))
  }
  if (t === 'Feature') return pointInBoundary(lon, lat, boundary.geometry)
  if (t === 'Polygon') return inPolygon(lon, lat, boundary.coordinates)
  if (t === 'MultiPolygon') {
    return (boundary.coordinates || []).some(poly => inPolygon(lon, lat, poly))
  }
  if (t === 'GeometryCollection') {
    return (boundary.geometries || []).some(g => pointInBoundary(lon, lat, g))
  }
  return false
}

/** 2) GPS match: the site containing the most stems, if it holds a majority. */
function matchByGps(stems, sites) {
  const located = stems.filter(s => s.lat != null && s.lon != null)
  if (located.length < 3) return null
  const withBoundary = sites.filter(s => s.boundary)
  if (!withBoundary.length) return null

  let best = null, bestCount = 0
  for (const site of withBoundary) {
    let count = 0
    for (const st of located) {
      if (pointInBoundary(st.lon, st.lat, site.boundary)) count++
    }
    if (count > bestCount) { best = site; bestCount = count }
  }
  if (best && bestCount >= Math.max(3, located.length * 0.5)) return best
  return null
}

/**
 * Resolve site + harvesting operation for a parsed HPR.
 * Returns { siteId, operationId, matchedBy } (all possibly null).
 */
async function resolveSite(supabase, hprData, sites) {
  let site = matchByName(hprData.objectName, sites)
  let matchedBy = site ? 'object_name' : null
  if (!site) {
    site = matchByGps(hprData.stems, sites)
    if (site) matchedBy = 'gps'
  }
  if (!site) return { siteId: null, operationId: null, matchedBy: null }

  // Link the site's harvesting operation so DeliveryPanel picks the stems up:
  // an in-progress harvesting op wins; otherwise a single planned one.
  let operationId = null
  const { data: ops } = await supabase
    .from('site_operations')
    .select('id,status,type')
    .eq('site_id', site.id)
    .eq('type', 'harvesting')
  const list = ops || []
  const inProgress = list.filter(o => o.status === 'in_progress')
  if (inProgress.length === 1) operationId = inProgress[0].id
  else if (inProgress.length === 0 && list.length === 1) operationId = list[0].id

  return { siteId: site.id, operationId, matchedBy }
}

module.exports = { loadLeafSites, resolveSite, matchByName, matchByGps, pointInBoundary }
