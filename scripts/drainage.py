"""
drainage.py  –  Stream & ditch network from the CSF ground surface (ground.csv).

Stage of the Arrol algorithmic pipeline. Invoked by the worker registry as:

  python3 drainage.py \
      --input       <shared>/ground.csv \
      --out-tif     <shared>/drainage.tif \
      --out-summary <shared>/drainage_summary.json

Builds the same continuous DTM as dtm.py / slope.py (interpolated ground surface
inside the data footprint), then derives where water concentrates:

  1.  Priority-Flood + epsilon (Barnes, Lehman & Mulla 2014) fills pits and removes
      flats, so every ground cell has a monotonic downslope path to the survey edge.
  2.  D8 steepest-descent flow directions on that conditioned surface.
  3.  Flow accumulation = how many upslope cells drain through each cell -> its
      contributing area in m2.
  4.  Cells whose contributing area exceeds a threshold form the drainage network
      (natural streams plus cut ditches that collect upslope flow). The raster value
      is log10(contributing area), so main channels read darker than minor ditches.

Output is a single-band GeoTIFF (EPSG:27700, nodata off-network); the worker
colour-ramps it blue (faint -> deep) and reprojects to 3857 for the map.
"""

import argparse
import json
import heapq

import numpy as np

NODATA = -9999.0
_S2 = float(np.sqrt(2.0))
# 8 neighbours (dr, dc) and their centre-to-centre distance in cell widths.
_OFFS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
_DIST = [_S2, 1.0, _S2, 1.0, 1.0, _S2, 1.0, _S2]


def _neighbor(a, dr, dc, fill):
    """Array S where S[r,c] = a[r+dr, c+dc], out-of-bounds filled with `fill`."""
    nr, nc = a.shape
    res = np.full((nr, nc), fill, dtype=float)
    rs0, rs1 = max(0, -dr), nr - max(0, dr)
    cs0, cs1 = max(0, -dc), nc - max(0, dc)
    ra0, ra1 = max(0, dr), nr - max(0, -dr)
    ca0, ca1 = max(0, dc), nc - max(0, -dc)
    res[rs0:rs1, cs0:cs1] = a[ra0:ra1, ca0:ca1]
    return res


def priority_flood_eps(dem, valid, eps=1e-3):
    """Depression-fill + flat-removal. Returns a conditioned surface (nan off-data)."""
    nrows, ncols = dem.shape
    filled = np.full(dem.shape, np.nan)
    closed = ~valid.copy()                       # nodata cells act as walls / outlets

    # Seed the queue with every data cell on the grid border or touching nodata —
    # the whole survey perimeter is a valid outlet, not just the rectangle edge.
    border = np.zeros(dem.shape, bool)
    border[0, :] = border[-1, :] = border[:, 0] = border[:, -1] = True
    nod = ~valid
    adj_nod = np.zeros(dem.shape, bool)
    for dr, dc in _OFFS:
        adj_nod |= _neighbor(nod.astype(float), dr, dc, 1.0) > 0.5   # OOB counts as nodata
    seed = valid & (border | adj_nod)

    heap = []
    for r, c in zip(*np.where(seed)):
        closed[r, c] = True
        filled[r, c] = dem[r, c]
        heapq.heappush(heap, (float(dem[r, c]), int(r), int(c)))

    while heap:
        z, r, c = heapq.heappop(heap)
        for dr, dc in _OFFS:
            nr, nc = r + dr, c + dc
            if 0 <= nr < nrows and 0 <= nc < ncols and not closed[nr, nc]:
                closed[nr, nc] = True
                d = dem[nr, nc]
                nz = d if d > z + eps else z + eps   # raise pits/flats just enough to drain
                filled[nr, nc] = nz
                heapq.heappush(heap, (float(nz), nr, nc))
    return filled


def d8_downstream(filled, valid):
    """Flat index of each cell's D8 downstream neighbour (-1 = outlet)."""
    nrows, ncols = filled.shape
    best_slope = np.zeros(filled.shape)
    best_k = np.full(filled.shape, -1, dtype=np.int64)
    for k, (dr, dc) in enumerate(_OFFS):
        nz = _neighbor(filled, dr, dc, np.inf)
        nv = _neighbor(valid.astype(float), dr, dc, 0.0) > 0.5
        slope = (filled - nz) / _DIST[k]
        cand = valid & nv & np.isfinite(slope) & (slope > best_slope)
        best_slope = np.where(cand, slope, best_slope)
        best_k = np.where(cand, k, best_k)

    down = np.full(nrows * ncols, -1, dtype=np.int64)
    rows, cols = np.indices(filled.shape)
    for k, (dr, dc) in enumerate(_OFFS):
        sel = best_k == k
        if sel.any():
            r, c = rows[sel], cols[sel]
            down[r * ncols + c] = (r + dr) * ncols + (c + dc)
    return down


def flow_accumulation(filled, valid, down):
    """Cells draining through each cell (incl. itself), processed high -> low."""
    fr = filled.ravel()
    validr = valid.ravel()
    acc = validr.astype(np.float64)                       # each valid cell counts itself
    order = np.argsort(np.where(np.isfinite(fr), fr, -np.inf))[::-1]
    for idx in order:
        if not validr[idx]:
            break                                         # remaining cells are nodata
        d = down[idx]
        if d >= 0:
            acc[d] += acc[idx]
    return acc.reshape(filled.shape)


def compute_drainage(dem, valid, res, min_area_m2):
    """DEM -> (stream raster value, contributing-area raster, stats)."""
    filled = priority_flood_eps(dem, valid)
    down = d8_downstream(filled, valid)
    accum = flow_accumulation(filled, valid, down)
    area = accum * (res * res)                            # contributing area, m2
    streams = valid & (area >= min_area_m2)
    value = np.where(streams, np.log10(np.maximum(area, 1.0)), NODATA).astype(np.float32)
    stats = {
        'stream_cells': int(streams.sum()),
        'max_contributing_area_ha': round(float(area[valid].max()) / 1e4, 2) if valid.any() else 0.0,
    }
    return value, area, streams, stats


def _grid_dem(input_csv, res):
    """Grid ground.csv (x,y,z) to a DEM, matching dtm.py / slope.py footprint logic."""
    from scipy.interpolate import griddata
    from rasterio.transform import from_origin
    g = np.loadtxt(input_csv, delimiter=',', skiprows=1)
    if g.ndim == 1:
        g = g.reshape(1, -1)
    gx, gy, gz = g[:, 0], g[:, 1], g[:, 2]
    if len(gz) < 4:
        raise SystemExit("ground.csv has too few points to build a DTM.")
    x_min, y_min, x_max, y_max = gx.min(), gy.min(), gx.max(), gy.max()
    ncols = int((x_max - x_min) / res) + 1
    nrows = int((y_max - y_min) / res) + 1
    col_centres = x_min + (np.arange(ncols) + 0.5) * res
    row_centres = y_max - (np.arange(nrows) + 0.5) * res
    grid_x, grid_y = np.meshgrid(col_centres, row_centres)
    dem = griddata((gx, gy), gz, (grid_x, grid_y), method='linear')   # nan outside footprint
    valid = ~np.isnan(dem)
    if not valid.any():
        raise SystemExit("DTM interpolation produced no valid cells.")
    transform = from_origin(x_min, y_max, res, res)
    return dem, valid, transform, nrows, ncols


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--input',       required=True, help='Ground surface CSV (x,y,ground_z).')
    ap.add_argument('--out-tif',     required=True, help='Output single-band drainage GeoTIFF.')
    ap.add_argument('--out-summary', required=True, help='Output summary .json.')
    ap.add_argument('--resolution',  type=float, default=0.5, help='Cell size (m). Default 0.5.')
    ap.add_argument('--min-drainage-area-m2', type=float, default=750.0,
                    help='Contributing area (m2) above which a cell is part of the network. '
                         'Lower = finer (more ditches), higher = only major channels. Default 750.')
    ap.add_argument('--crs', default='EPSG:27700',
                    help='CRS of the ground X/Y (UK national grid). Worker reprojects to 3857.')
    a = ap.parse_args()

    import rasterio

    print(f"Reading ground surface {a.input} ...")
    dem, valid, transform, nrows, ncols = _grid_dem(a.input, a.resolution)
    print(f"  Grid: {nrows} x {ncols} px at {a.resolution} m  ({int(valid.sum()):,} ground cells)")

    print("Conditioning surface + computing flow accumulation ...")
    value, area, streams, stats = compute_drainage(dem, valid, a.resolution, a.min_drainage_area_m2)

    with rasterio.open(a.out_tif, 'w', driver='GTiff', height=nrows, width=ncols,
                       count=1, dtype='float32', crs=a.crs, transform=transform,
                       nodata=NODATA, compress='deflate') as dst:
        dst.write(value, 1)

    summary = {
        'cell_size_m': a.resolution,
        'min_drainage_area_m2': a.min_drainage_area_m2,
        'stream_cells': stats['stream_cells'],
        'network_length_m_est': round(stats['stream_cells'] * a.resolution, 0),
        'max_contributing_area_ha': stats['max_contributing_area_ha'],
        'method': 'D8 flow accumulation (priority-flood + epsilon) on the DTM',
    }
    with open(a.out_summary, 'w') as f:
        json.dump(summary, f)
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
