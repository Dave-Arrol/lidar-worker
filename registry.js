const path = require('path')

/*
 * ANALYSIS REGISTRY — models the real algorithmic pipeline as a dependency graph.
 *
 * Execution model: all stages share ONE working folder (ctx.dir). Each stage reads
 * named artifacts produced by earlier stages and writes its own. ctx.f(name) resolves
 * a path inside that shared folder; ctx.cloud is the uploaded source cloud.
 *
 * To add an analysis later (DTM/DSM/CHM raster, density, etc.):
 *   1. drop a CLI script in scripts/ (argparse: reads --input, writes its outputs)
 *   2. add an entry here with dependsOn + args + presented outputs (by role)
 *
 * Presented output roles:
 *   'points'  -> Potree octree overlay     'table'  -> CSV (download + analytics)
 *   'summary' -> JSON merged to stat cards  'raster' -> COG on the map (future GeoTIFF scripts)
 */
const REGISTRY = [
  {
    id: 'normalise',
    label: 'Normalise (ground filter + height)',
    description: 'CSF ground filter, then height-above-ground normalisation.',
    dependsOn: [],
    script: 'normalise.py',
    args: (c) => ['--input', c.cloud,
                  '--out-las', c.f('normalised.las'),
                  '--out-ground', c.f('ground.csv')],
    outputs: [
      { role: 'table', file: 'ground.csv', name: 'Ground surface' },
    ],
  },
  {
    id: 'chm',
    label: 'Canopy height model',
    description: 'Max canopy height raster from the normalised cloud.',
    dependsOn: ['normalise'],
    script: 'chm.py',
    args: (c) => ['--input', c.f('normalised.las'),
                  '--out-tif', c.f('chm.tif'),
                  '--out-summary', c.f('chm_summary.json')],
    outputs: [
      { role: 'raster',  file: 'chm.tif',          name: 'Canopy height', kind: 'chm' },
      { role: 'summary', file: 'chm_summary.json', name: 'summary' },
    ],
  },
  {
    id: 'dtm',
    label: 'DTM (hillshade + colour)',
    description: 'Ground-surface elevation map + multidirectional hillshade.',
    dependsOn: ['normalise'],
    script: 'dtm.py',
    // mode tells the worker which colour-relief ramp to use: 'terrain' vs 'grey'
    args: (c) => ['--input', c.f('ground.csv'),
                  '--out-colour', c.f('dtm_colour.tif'),
                  '--out-hillshade', c.f('dtm_hillshade.tif'),
                  '--out-summary', c.f('dtm_summary.json')],
    outputs: [
      { role: 'raster',  file: 'dtm_colour.tif',    name: 'DTM elevation', kind: 'dtm',       mode: 'terrain' },
      { role: 'raster',  file: 'dtm_hillshade.tif', name: 'DTM hillshade', kind: 'hillshade', mode: 'grey' },
      { role: 'summary', file: 'dtm_summary.json',   name: 'summary' },
    ],
  },
  {
    id: 'slope',
    label: 'Slope analysis',
    description: 'Ground slope in degrees, derived from the DTM.',
    dependsOn: ['normalise'],
    script: 'slope.py',
    args: (c) => ['--input', c.f('ground.csv'),
                  '--out-tif', c.f('slope.tif'),
                  '--out-summary', c.f('slope_summary.json')],
    outputs: [
      { role: 'raster',  file: 'slope.tif',          name: 'Slope', kind: 'slope', mode: 'slope' },
      { role: 'summary', file: 'slope_summary.json', name: 'summary' },
    ],
  },
  {
    id: 'treetops',
    label: 'Tree tops (quick)',
    description: 'Lightweight standalone tree-top detection (no segmentation).',
    dependsOn: ['normalise'],
    script: 'extract_treetops.py',
    // writes *_std.* so it never clobbers the segmentation chain's treetops.csv
    args: (c) => ['--input', c.f('normalised.las'),
                  '--out-csv', c.f('treetops_std.csv'),
                  '--out-las', c.f('treetops_std.las'),
                  '--out-summary', c.f('treetops_std_summary.json'),
                  '--out-geojson', c.f('treetops_std.geojson')],
    outputs: [
      { role: 'vector',  file: 'treetops_std.geojson',      name: 'Tree tops', kind: 'treetops' },
      { role: 'points',  file: 'treetops_std.las',          name: 'Tree tops (3D)' },
      { role: 'table',   file: 'treetops_std.csv',          name: 'Tree tops table' },
      { role: 'summary', file: 'treetops_std_summary.json', name: 'summary' },
    ],
  },
  {
    id: 'density',
    label: 'Tree density',
    description: 'Stem density (trees/ha) on a 10 m grid, from the tree tops.',
    dependsOn: ['treetops'],
    script: 'tree_density.py',
    args: (c) => ['--input', c.f('treetops_std.csv'),
                  '--out-tif', c.f('tree_density.tif'),
                  '--out-summary', c.f('tree_density_summary.json')],
    outputs: [
      { role: 'raster',  file: 'tree_density.tif',          name: 'Tree density', kind: 'density', mode: 'density' },
      { role: 'summary', file: 'tree_density_summary.json', name: 'summary' },
    ],
  },
  {
    id: 'segmentation',
    label: 'Crown segmentation',
    description: 'Watershed tree assignment + lower-stem candidate cloud.',
    dependsOn: ['normalise'],
    script: 'segmentation_v2.py',
    args: (c) => ['--input', c.f('normalised.las'),
                  '--out-candidates', c.f('tree_candidates.las'),
                  '--out-treetops-las', c.f('treetops.las'),
                  '--out-treetops-csv', c.f('treetops.csv'),
                  '--out-summary-csv', c.f('segment_summary.csv')],
    outputs: [
      { role: 'points', file: 'tree_candidates.las', name: 'Stem candidates' },
      { role: 'points', file: 'treetops.las',        name: 'Tree-top poles' },
      { role: 'table',  file: 'segment_summary.csv', name: 'Segment summary' },
    ],
  },
  {
    id: 'dbh',
    label: 'DBH extraction',
    description: 'Per-tree diameter at breast height, slices and taper models.',
    dependsOn: ['segmentation'],   // + normalise (ground.csv) via the chain
    script: 'dbh_extraction_v3d.py',
    args: (c) => ['--input', c.f('tree_candidates.las'),
                  '--ground', c.f('ground.csv'),
                  '--treetops', c.f('treetops.csv'),
                  '--segment-summary', c.f('segment_summary.csv'),
                  '--out-results', c.f('results.csv'),
                  '--out-slices', c.f('dbh_slices.csv'),
                  '--out-models', c.f('taper_models.csv'),
                  '--out-las', c.f('dbh.las'),
                  '--out-summary', c.f('dbh_summary.json'),
                  '--out-geojson', c.f('tree_dbh.geojson')],
    outputs: [
      { role: 'points',  file: 'dbh.las',         name: 'DBH ring fits' },
      { role: 'table',   file: 'results.csv',     name: 'DBH results' },
      { role: 'table',   file: 'dbh_slices.csv',  name: 'DBH slices' },
      { role: 'table',   file: 'taper_models.csv',name: 'Taper models' },
      { role: 'vector',  file: 'tree_dbh.geojson',name: 'Tree DBH', kind: 'dbh' },
      { role: 'summary', file: 'dbh_summary.json',name: 'summary' },
    ],
  },
  {
    id: 'stem_profile',
    label: 'Stem profile',
    description: 'Radius-at-height profiles and per-stem summary.',
    dependsOn: ['dbh'],
    script: 'stem_profile.py',
    args: (c) => ['--results', c.f('results.csv'),
                  '--slices', c.f('dbh_slices.csv'),
                  '--models', c.f('taper_models.csv'),
                  '--treetops', c.f('treetops.csv'),
                  '--out-profile', c.f('stem_profile.csv'),
                  '--out-summary', c.f('stem_profile_summary.csv'),
                  '--out-las', c.f('stem_profile.las')],
    outputs: [
      { role: 'points', file: 'stem_profile.las',         name: 'Stem profile' },
      { role: 'table',  file: 'stem_profile.csv',         name: 'Stem profile' },
      { role: 'table',  file: 'stem_profile_summary.csv', name: 'Stem profile summary' },
    ],
  },
]

function publicRegistry() {
  return REGISTRY.map(a => ({
    id: a.id, label: a.label, description: a.description,
    dependsOn: a.dependsOn,
    outputs: a.outputs.map(o => ({ role: o.role, name: o.name })),
  }))
}

function resolveChain(ids) {
  const byId = Object.fromEntries(REGISTRY.map(a => [a.id, a]))
  const ordered = [], seen = new Set()
  const visit = (id) => {
    if (seen.has(id) || !byId[id]) return
    byId[id].dependsOn.forEach(visit)
    seen.add(id); ordered.push(byId[id])
  }
  ids.forEach(visit)
  return ordered
}

module.exports = { REGISTRY, publicRegistry, resolveChain }
