// komatsu.js — Komatsu SmartForestry StanForD File REST API client.
//
// The API is a file-sync surface, not a telemetry stream: LIST returns file names
// for a machine + date window; GET returns the raw StanForD XML. Auth is a single
// X-Api-Key header. File names encode the machine and timestamp:
//   {chassis}_{yyyymmdd}_{hhmmss}.{hpr|mom|fpr}
//
// Env:
//   KOMATSU_API_KEY   (required — from AWS Secrets Manager in the Batch job def)
//   KOMATSU_API_BASE  (default https://smartforestry.komatsuforest.com:6001/Stanford)

'use strict'

const BASE = (process.env.KOMATSU_API_BASE || 'https://smartforestry.komatsuforest.com:6001/Stanford')
  .replace(/\/+$/, '')
const V1 = `${BASE}/File/V1.0`

function apiKey() {
  const k = process.env.KOMATSU_API_KEY
  if (!k) throw new Error('KOMATSU_API_KEY not configured')
  return k
}

async function komatsuFetch(url, asBuffer = false) {
  const res = await fetch(url, { headers: { 'X-Api-Key': apiKey() } })
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`Komatsu ${res.status} ${url.replace(/\?.*$/, '')}: ${body.slice(0, 300)}`)
  }
  return asBuffer ? Buffer.from(await res.arrayBuffer()) : res.text()
}

// The list endpoints return XML: <Response ...><Entry>name</Entry>...</Response>.
// A tolerant regex is all that is needed — entries are plain text file names.
function parseEntries(xml) {
  const out = []
  const re = /<Entry\s*\/>|<Entry>([^<]*)<\/Entry>/g
  let m
  while ((m = re.exec(xml)) !== null) {
    const v = (m[1] || '').trim()
    if (v) out.push(v)
  }
  return out
}

// Komatsu examples use "2023-06-13T00:00:00.0Z"; standard ISO is accepted.
function isoZ(d) { return new Date(d).toISOString() }

/** List production files of a type ('HPR'|'MOM'|'FPR') in [start, end]. */
async function listFiles(type, start, end, machineId) {
  const p = new URLSearchParams({ StartDate: isoZ(start), EndDate: isoZ(end) })
  if (machineId) p.set('BaseMachineManufacturerID', String(machineId))
  const xml = await komatsuFetch(`${V1}/${type.toUpperCase()}?${p.toString()}`)
  return parseEntries(xml)
}

/** Download one file's raw StanForD XML bytes. */
async function getFile(type, fileName) {
  return komatsuFetch(`${V1}/${type.toUpperCase()}/${encodeURIComponent(fileName)}`, true)
}

/** Latest date a file was received from a machine (chassis number). */
async function syncStatus(chassis) {
  try {
    const xml = await komatsuFetch(`${V1}/status/syncronization/${encodeURIComponent(chassis)}`)
    return parseEntries(xml)[0] || null
  } catch {
    return null
  }
}

/** Parse "{chassis}_{yyyymmdd}_{hhmmss}.{ext}" into its parts. */
function parseFileName(name) {
  const m = /^(\d+)_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})\.(\w+)$/.exec(name)
  if (!m) return { chassis: null, timestamp: null, ext: (name.split('.').pop() || '').toLowerCase() }
  const [, chassis, y, mo, d, h, mi, s, ext] = m
  return {
    chassis,
    timestamp: `${y}-${mo}-${d}T${h}:${mi}:${s}Z`,
    ext: ext.toLowerCase(),
  }
}

module.exports = { listFiles, getFile, syncStatus, parseFileName }
