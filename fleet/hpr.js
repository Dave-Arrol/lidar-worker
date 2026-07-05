// hpr.js — StanForD 2010 .hpr parser for the fleet sync worker.
//
// A direct Node port of the portal's proven lib/hpr.ts (same logic, same field
// mapping), using @xmldom/xmldom in place of the browser DOMParser. If the parse
// logic ever changes, change it in BOTH places — lib/hpr.ts is the reference.

'use strict'

const { DOMParser } = require('@xmldom/xmldom')

// ── DOM helpers ───────────────────────────────────────────────────────────────
// All elements use the stanford2010 default namespace, so match by localName.
function getEls(parent, tag) {
  const all = parent.getElementsByTagName('*')
  const out = []
  for (let i = 0; i < all.length; i++) {
    if (all[i].localName === tag) out.push(all[i])
  }
  return out
}

function childText(parent, tag) {
  const el = getEls(parent, tag)[0]
  return el && el.textContent ? el.textContent.trim() : ''
}

function numChild(parent, tag) {
  return parseFloat(childText(parent, tag) || '0') || 0
}

// LogVolume with category preference: JD TimberMatic writes several volumes
// per log (m3 (price), m3sob, m3sub); Komatsu writes one uncategorised value.
// m3sob (solid over bark) matches harvested_stems volume_ob semantics.
function logVolume(logEl) {
  const els = getEls(logEl, 'LogVolume')
  if (!els.length) return 0
  for (const cat of ['m3sob', 'm3 (price)', 'm3sub']) {
    const el = els.find(e => e.getAttribute('logVolumeCategory') === cat)
    if (el) return parseFloat(el.textContent || '0') || 0
  }
  return parseFloat(els[0].textContent || '0') || 0
}

function logDiam(logEl, cat) {
  const el = getEls(logEl, 'LogDiameter').find(e => e.getAttribute('logDiameterCategory') === cat)
  return parseFloat((el && el.textContent) || '0') || 0
}

function coordsFrom(el) {
  const latEl = getEls(el, 'Latitude')[0]
  const lonEl = getEls(el, 'Longitude')[0]
  if (!latEl || !lonEl) return { lat: null, lon: null }
  const lat = parseFloat(latEl.textContent || '0') || 0
  const lon = parseFloat(lonEl.textContent || '0') || 0
  const lonSigned = lonEl.getAttribute('longitudeCategory') === 'West' ? -lon : lon
  const latSigned = latEl.getAttribute('latitudeCategory') === 'South' ? -lat : lat
  return {
    lat: latSigned !== 0 ? latSigned : null,
    lon: lonSigned !== 0 ? lonSigned : null,
  }
}

// ── Parser ────────────────────────────────────────────────────────────────────
function parseHpr(xml, fileName) {
  try {
    xml = String(xml).replace(/^\uFEFF/, '')
    let fatal = null
    const doc = new DOMParser({
      onError: (level, msg) => { if (level === 'fatalError') fatal = msg },
    }).parseFromString(xml, 'application/xml')
    if (fatal || !doc || !doc.documentElement) return null
    const root = doc.documentElement

    const machineId   = childText(root, 'MachineUserID')
    const machineName = childText(root, 'MachineBaseModel') || childText(root, 'MachineBaseManufacturer')
    const objectName  = childText(root, 'ObjectName')
    const startDate   = (childText(root, 'StartTime') || childText(root, 'HarvestDate') || childText(root, 'StartDate')).slice(0, 10)
    const endDate     = (childText(root, 'StopTime') || childText(root, 'EndDate') || '').slice(0, 10)

    const speciesMap = {}
    getEls(root, 'SpeciesGroupDefinition').forEach(el => {
      const key = childText(el, 'SpeciesGroupKey')
      const name = childText(el, 'SpeciesGroupName')
      if (key) speciesMap[key] = name
    })

    const productMap = {}
    getEls(root, 'ProductDefinition').forEach(el => {
      const key = childText(el, 'ProductKey')
      const name = childText(el, 'ProductName')
      if (key) productMap[key] = name
    })

    const stems = getEls(root, 'Stem').map(stemEl => {
      const speciesCode = childText(stemEl, 'SpeciesGroupKey')
      const species = speciesMap[speciesCode] || speciesCode

      // GPS — prefer crane tip (felling position), fall back to base machine
      let lat = null, lon = null
      const coordEls = getEls(stemEl, 'StemCoordinates')
      const craneEl = coordEls.find(c => {
        const rp = c.getAttribute('receiverPosition')
        return rp && rp.toLowerCase().includes('crane')
      })
      const useCoord = craneEl || coordEls[0]
      if (useCoord) ({ lat, lon } = coordsFrom(useCoord))

      const stpEl = getEls(stemEl, 'SingleTreeProcessedStem')[0]
      const dbhMM = stpEl ? numChild(stpEl, 'DBH') : 0
      const taper = []
      if (stpEl) {
        const sdEl = getEls(stpEl, 'StemDiameters')[0]
        if (sdEl) {
          getEls(sdEl, 'DiameterValue').forEach(dv => {
            const posM = parseFloat(dv.getAttribute('diameterPosition') || '0') / 100
            const diamMM = parseFloat(dv.textContent || '0') || 0
            if (diamMM > 0) taper.push({ posM, diamMM })
          })
        }
      }
      const heightM = taper.length ? taper[taper.length - 1].posM : 0

      const logEls = stpEl ? getEls(stpEl, 'Log') : getEls(stemEl, 'Log')
      const logs = logEls.map(lEl => {
        const vol = logVolume(lEl)
        const startRaw = childText(lEl, 'StartPos')

        let logLat = null, logLon = null
        const lcEl = getEls(lEl, 'LogCoordinates')[0]
        if (lcEl) ({ lat: logLat, lon: logLon } = coordsFrom(lcEl))

        return {
          logKey:     childText(lEl, 'LogKey'),
          product:    productMap[childText(lEl, 'ProductKey')] || childText(lEl, 'ProductKey'),
          startM:     startRaw ? parseFloat(startRaw) / 100 : null,
          lengthM:    numChild(lEl, 'LogLength') / 100,
          diamButtMM: logDiam(lEl, 'Butt ob') || logDiam(lEl, 'Butt ub'),
          diamTopMM:  logDiam(lEl, 'Top ob') || logDiam(lEl, 'Top ub'),
          volumeM3:   vol,
          logLat,
          logLon,
        }
      })

      // JD TimberMatic omits StartPos — rebuild cumulative positions so the
      // cut plan and profile still stack correctly (explicit positions kept).
      let cursorM = 0
      for (const l of logs) {
        if (l.startM == null) l.startM = cursorM
        cursorM = l.startM + l.lengthM
      }
      const stemHeightM = heightM || cursorM

      const stemVol = logs.reduce((a, l) => a + l.volumeM3, 0)

      return {
        stemKey: childText(stemEl, 'StemKey'),
        stemNumber: childText(stemEl, 'StemNumber') || childText(stemEl, 'StemKey'),
        species, speciesCode,
        dbhMM, heightM: stemHeightM, volumeM3: stemVol, lat, lon, logs, taper,
      }
    })

    return {
      fileName, machineId, machineName, objectName, startDate, endDate, stems,
      totalVolume: Math.round(stems.reduce((a, s) => a + s.volumeM3, 0) * 1000) / 1000,
      totalStems: stems.length,
    }
  } catch (e) {
    console.error('[hpr] parse error', fileName, e && e.message)
    return null
  }
}

// ── Map parsed stems to harvested_stems insert rows ───────────────────────────
// Mirrors lib/hpr.ts stemsToHarvestRows, extended for the machine feed:
// operation_id may be null, vendor / machine_file_id carry provenance, and each
// row stores the full taper profile plus per-log detail (start position, butt
// and top diameters) so the portal can draw stem profiles and grade log
// quality without re-reading the source XML.
function stemsToHarvestRows(data, opts) {
  const { operationId, siteId, machineFileId, vendor, felledAtIso } = opts
  const felled = felledAtIso || (data.startDate ? `${data.startDate}T12:00:00Z` : null)
  return data.stems.map(s => {
    const byProduct = {}
    for (const l of s.logs) byProduct[l.product] = (byProduct[l.product] || 0) + l.volumeM3
    const sorted = Object.entries(byProduct).sort((a, b) => b[1] - a[1])
    const primary = (sorted[0] && sorted[0][0]) || ''
    return {
      operation_id: operationId || null,
      site_id: siteId || null,
      stem_number: parseInt(s.stemNumber || s.stemKey, 10) || null,
      species: s.species || '',
      dbh_mm: s.dbhMM ? Math.round(s.dbhMM) : null,
      length_dm: s.heightM ? Math.round(s.heightM * 10) : null,
      volume_ob_m3: Math.round(s.volumeM3 * 1000) / 1000,
      volume_ub_m3: null,
      assortment: primary,
      logs: s.logs.map(l => ({
        log_key: l.logKey || '',
        assortment: l.product,
        start_cm: Math.round(l.startM * 100),
        length_cm: Math.round(l.lengthM * 100),
        diam_butt_mm: l.diamButtMM ? Math.round(l.diamButtMM) : null,
        diam_top_mm: l.diamTopMM ? Math.round(l.diamTopMM) : null,
        volume_m3: Math.round(l.volumeM3 * 1000) / 1000,
      })),
      taper: s.taper.map(t => ({ pos_cm: Math.round(t.posM * 100), diam_mm: Math.round(t.diamMM) })),
      longitude: s.lon,
      latitude: s.lat,
      machine: data.machineId || data.machineName || '',
      felled_at: felled,
      source_file: data.fileName,
      machine_file_id: machineFileId || null,
      vendor: vendor || '',
    }
  })
}

module.exports = { parseHpr, stemsToHarvestRows }
