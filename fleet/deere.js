// deere.js — John Deere Operations Center adapter for the fleet sync worker.
//
// The OAuth *connect* flow stays in the portal (user-interactive); this module
// consumes the stored connection from Supabase (deere_connections), refreshes the
// access token when needed (JD tokens last 12h; the scheduled sync keeps the
// refresh chain alive), then pulls the equipment list and latest machine location
// via HATEOAS link discovery — mirroring the portal's lib/deere.ts.
//
// Production access for Equipment + Machine Locations was granted under JD ticket
// 4295520, so the default API base is the production host. Override with
// DEERE_API_BASE if you ever need sandbox again.
//
// Env:
//   DEERE_CLIENT_ID / DEERE_CLIENT_SECRET  (required — from Secrets Manager)
//   DEERE_OAUTH_BASE  (default https://signin.johndeere.com/oauth2/aus78tnlaysMraFhC1t7)
//   DEERE_API_BASE    (default https://partnerapi.deere.com/platform)

'use strict'

const OAUTH_BASE = (process.env.DEERE_OAUTH_BASE || 'https://signin.johndeere.com/oauth2/aus78tnlaysMraFhC1t7').replace(/\/+$/, '')
const API_BASE   = (process.env.DEERE_API_BASE || 'https://partnerapi.deere.com/platform').replace(/\/+$/, '')
const ACCEPT     = 'application/vnd.deere.axiom.v3+json'

function basicAuth() {
  const id = process.env.DEERE_CLIENT_ID || ''
  const secret = process.env.DEERE_CLIENT_SECRET || ''
  if (!id || !secret) throw new Error('DEERE_CLIENT_ID / DEERE_CLIENT_SECRET not configured')
  return 'Basic ' + Buffer.from(`${id}:${secret}`).toString('base64')
}

async function refreshToken(refreshTokenValue) {
  const res = await fetch(`${OAUTH_BASE}/v1/token`, {
    method: 'POST',
    headers: {
      'Authorization': basicAuth(),
      'Content-Type': 'application/x-www-form-urlencoded',
      'Accept': 'application/json',
    },
    body: new URLSearchParams({ grant_type: 'refresh_token', refresh_token: refreshTokenValue }).toString(),
  })
  if (!res.ok) throw new Error(`JD token refresh ${res.status}: ${(await res.text()).slice(0, 300)}`)
  return res.json()
}

async function jdGet(token, url) {
  const res = await fetch(url, { headers: { 'Authorization': `Bearer ${token}`, 'Accept': ACCEPT } })
  if (!res.ok) throw new Error(`JD ${res.status} ${url}: ${(await res.text()).slice(0, 300)}`)
  return res.json()
}

function linkUri(obj, rel) {
  const l = ((obj && obj.links) || []).find(x => x.rel === rel)
  return (l && l.uri) || null
}

/**
 * Load the newest deere_connections row and ensure a valid access token,
 * persisting any refresh back to Supabase. Returns { conn, token } or null.
 */
async function getValidConnection(supabase) {
  const { data: conn } = await supabase
    .from('deere_connections')
    .select('*')
    .order('created_at', { ascending: false })
    .limit(1)
    .maybeSingle()
  if (!conn || !conn.access_token) return null

  const exp = conn.token_expires_at ? new Date(conn.token_expires_at).getTime() : 0
  if (exp - Date.now() > 60000) return { conn, token: conn.access_token }
  if (!conn.refresh_token) return { conn, token: conn.access_token }

  const t = await refreshToken(conn.refresh_token)
  await supabase.from('deere_connections').update({
    access_token: t.access_token,
    refresh_token: t.refresh_token || conn.refresh_token,
    token_expires_at: new Date(Date.now() + (t.expires_in || 43200) * 1000).toISOString(),
    updated_at: new Date().toISOString(),
  }).eq('id', conn.id)
  return { conn, token: t.access_token }
}

/** HATEOAS-first equipment list for an organisation. */
async function listMachines(token, orgId) {
  let url = `${API_BASE}/organizations/${orgId}/machines`
  try {
    const org = await jdGet(token, `${API_BASE}/organizations/${orgId}`)
    const link = ((org && org.links) || []).find(l => l.rel === 'machines' || l.rel === 'equipment')
    if (link && link.uri) url = link.uri
  } catch { /* fall back to the constructed URL */ }
  const data = await jdGet(token, url)
  return data.values || []
}

/** Latest location for one machine, or null. */
async function latestLocation(token, machine) {
  const uri = linkUri(machine, 'locationHistory') ||
    (machine.id ? `${API_BASE}/machines/${machine.id}/locationHistory` : null)
  if (!uri) return null
  try {
    const data = await jdGet(token, uri)
    const v = (data.values || [])[0]
    if (!v || !v.point) return null
    return { lat: v.point.lat, lon: v.point.lon, at: v.eventTimestamp || null }
  } catch {
    return null
  }
}

/** Rough machine-kind classification from JD category/model text. */
function classifyKind(m) {
  const text = `${m.category || ''} ${m.equipmentApexType || ''} ${m.equipmentModel || m.model || ''}`.toLowerCase()
  if (/harvester|wheeled harvester|tracked harvester/.test(text)) return 'harvester'
  if (/forwarder/.test(text)) return 'forwarder'
  if (/skidder|feller|swing machine|loader/.test(text)) return 'other'
  return 'unknown'
}

module.exports = { getValidConnection, listMachines, latestLocation, classifyKind }
