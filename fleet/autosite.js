// autosite.js — provision Arrol sites automatically from the machine feed.
//
// When an HPR arrives for a harvest object that has no matching site, the
// machines are telling us where work is happening before anyone has set the
// site up. Rather than leaving production unassigned, we create the site:
//
//   name      = ObjectName as keyed into the machine
//   kind      = 'site', parented under a dedicated "Machine feed" estate so it
//               appears in the portfolio tree and can be re-parented later
//   boundary  = convex hull of the stem felling positions, buffered ~25 m —
//               a real, honest approximation of the coupe from the data itself
//   notes     = provenance, so nobody mistakes it for a surveyed boundary
//
// Files with no ObjectName are left unassigned — we never invent names.

'use strict'

const FEED_ESTATE_NAME = 'Machine feed'
const BUFFER_M = 25

// ── Geometry (WGS84, local-metres approximation around the centroid) ─────────
function centroidOf(points) {
  let lat = 0, lon = 0
  for (const p of points) { lat += p.lat; lon += p.lon }
  return { lat: lat / points.length, lon: lon / points.length }
}

// Andrew's monotone chain convex hull on [x, y] pairs.
function convexHull(pts) {
  const P = pts.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1])
  if (P.length <= 2) return P
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
  const lower = []
  for (const p of P) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop()
    lower.push(p)
  }
  const upper = []
  for (let i = P.length - 1; i >= 0; i--) {
    const p = P[i]
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop()
    upper.push(p)
  }
  lower.pop(); upper.pop()
  return lower.concat(upper)
}

/**
 * Approximate coupe boundary from stem positions: project to local metres,
 * hull, push each vertex outward BUFFER_M from the centroid, project back.
 * Returns a GeoJSON Polygon, or null with fewer than 3 usable points.
 */
function boundaryFromStems(stems) {
  const pts = stems.filter(s => s.lat != null && s.lon != null)
  if (pts.length < 3) return null
  const c = centroidOf(pts)
  const mPerDegLat = 111320
  const mPerDegLon = 111320 * Math.cos(c.lat * Math.PI / 180)

  const xy = pts.map(p => [(p.lon - c.lon) * mPerDegLon, (p.lat - c.lat) * mPerDegLat])
  let hull = convexHull(xy)
  if (hull.length < 3) return null

  const ring = hull.map(([x, y]) => {
    const d = Math.hypot(x, y) || 1
    const f = (d + BUFFER_M) / d
    const bx = x * f, by = y * f
    return [c.lon + bx / mPerDegLon, c.lat + by / mPerDegLat]
  })
  ring.push(ring[0]) // close it
  return { type: 'Polygon', coordinates: [ring] }
}

/**
 * Pure (unbuffered) convex hull of lat/lon points as a GeoJSON Polygon, or
 * null with fewer than 3 usable points. feed_objects.hull stores THIS —
 * buffering is applied only when a site boundary is derived from it, so
 * repeated hull merges never inflate the shape.
 */
function hullFromLatLon(points) {
  const pts = (points || []).filter(p => p && p.lat != null && p.lon != null)
  if (pts.length < 3) return null
  const c = centroidOf(pts)
  const mPerDegLat = 111320
  const mPerDegLon = 111320 * Math.cos(c.lat * Math.PI / 180)
  const xy = pts.map(p => [(p.lon - c.lon) * mPerDegLon, (p.lat - c.lat) * mPerDegLat])
  const hull = convexHull(xy)
  if (hull.length < 3) return null
  const ring = hull.map(([x, y]) => [c.lon + x / mPerDegLon, c.lat + y / mPerDegLat])
  ring.push(ring[0])
  return { type: 'Polygon', coordinates: [ring] }
}

// ── Supabase provisioning ─────────────────────────────────────────────────────
let feedEstateId = null

/** The "Machine feed" estate all auto-created sites live under (created once). */
async function ensureFeedEstate(supabase) {
  if (feedEstateId) return feedEstateId
  const { data: existing } = await supabase.from('sites')
    .select('id').eq('name', FEED_ESTATE_NAME).eq('kind', 'estate')
    .is('deleted_at', null).limit(1).maybeSingle()
  if (existing) { feedEstateId = existing.id; return feedEstateId }

  // Auto sites should sit beside curated ones; reuse an existing owner if any.
  const { data: anySite } = await supabase.from('sites')
    .select('user_id').not('user_id', 'is', null).limit(1).maybeSingle()

  const { data: created, error } = await supabase.from('sites').insert({
    name: FEED_ESTATE_NAME,
    kind: 'estate',
    user_id: anySite ? anySite.user_id : null,
    notes: 'Sites in this estate were created automatically from machine production data. Re-parent them into their proper estates when convenient.',
  }).select('id').single()
  if (error) throw new Error('feed estate create failed: ' + error.message)
  console.log('[autosite] created "Machine feed" estate')
  feedEstateId = created.id
  return feedEstateId
}

/**
 * Create a site for a harvest object seen in the feed.
 * stems: [{lat, lon}] for the boundary; vendor + machineLabel for provenance.
 * Returns the new site row ({ id, name, fls_reference, kind, boundary }).
 */
async function autoCreateSite(supabase, objectName, stems, vendor, machineLabel) {
  const name = String(objectName || '').trim()
  if (!name) return null

  const parentId = await ensureFeedEstate(supabase)
  const boundary = boundaryFromStems(stems)
  const c = boundary ? centroidOf(stems.filter(s => s.lat != null && s.lon != null)) : null

  const { data: created, error } = await supabase.from('sites').insert({
    name,
    kind: 'site',
    parent_id: parentId,
    boundary,
    latitude: c ? Math.round(c.lat * 1e6) / 1e6 : 0,
    longitude: c ? Math.round(c.lon * 1e6) / 1e6 : 0,
    user_id: null,
    notes: `Auto-created from ${vendor} machine feed (${machineLabel || 'unknown machine'}). ` +
      (boundary
        ? `Boundary is approximate — a ${BUFFER_M} m buffered hull of stem felling positions, not a surveyed boundary.`
        : 'No GPS in the source file, so no boundary was derived.'),
  }).select('id,name,fls_reference,kind,boundary').single()
  if (error) throw new Error('auto site create failed: ' + error.message)
  console.log(`[autosite] created site "${name}"${boundary ? ' with derived boundary' : ''}`)
  return created
}

module.exports = { autoCreateSite, ensureFeedEstate, boundaryFromStems, hullFromLatLon }
