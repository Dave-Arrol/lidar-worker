#!/usr/bin/env python3
"""
copc_clip.py - Arrol LiDAR: range-read a spatial subset from a COPC.

A COPC is octree-indexed, so a spatial crop only reads the nodes overlapping the
requested area. When the source is remote this means HTTP range reads, so pulling
one compartment out of a multi-GB cloud fetches only a few MB - never the whole file.

The --input source can be:
  - a local file : C:\\lidar\\cloud_merged.copc.laz
  - an S3 object : s3://arrol-lidar/copc/strachur/cloud_merged.copc.laz
  - an https URL : https://arrol-lidar.s3.eu-west-2.amazonaws.com/copc/.../cloud_merged.copc.laz

Crop by bounding box (coordinates in the COPC's own CRS):
  python copc_clip.py --input C:\\lidar\\cloud_merged.copc.laz \\
      --bounds "210000,700000,210500,700500" --out clip.laz --stats

Crop by polygon (GeoJSON; reprojected to the COPC CRS when --polygon-crs differs):
  python copc_clip.py --input s3://arrol-lidar/copc/strachur/cloud_merged.copc.laz \\
      --polygon compartment.geojson --polygon-crs EPSG:4326 --copc-crs EPSG:27700 \\
      --out clip.laz --stats

Requires the `pdal` command-line tool on PATH (installed alongside untwine via conda).
pyproj is only needed when reprojecting a polygon (--polygon-crs different from --copc-crs).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _pdal_info_summary(path):
    r = _run(["pdal", "info", "--summary", path])
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def _bounds_to_pdal(minx, miny, maxx, maxy):
    # PDAL bounds string form: ([minx, maxx], [miny, maxy])
    return "([{}, {}], [{}, {}])".format(minx, maxx, miny, maxy)


def _ring_wkt(ring):
    return "(" + ", ".join("{} {}".format(x, y) for x, y in ring) + ")"


def _load_polygon_wkt(geojson_path, src_crs, dst_crs):
    with open(geojson_path) as f:
        gj = json.load(f)

    geom = gj
    if gj.get("type") == "FeatureCollection":
        feats = gj.get("features") or []
        if not feats:
            sys.exit("GeoJSON FeatureCollection has no features.")
        geom = feats[0]["geometry"]
    elif gj.get("type") == "Feature":
        geom = gj["geometry"]

    gtype = geom["type"]
    coords = geom["coordinates"]

    reproject = bool(src_crs and dst_crs and src_crs.upper() != dst_crs.upper())
    if reproject:
        try:
            from pyproj import Transformer
        except ImportError:
            sys.exit(
                "pyproj is required to reproject the polygon. Install it "
                "(conda install -c conda-forge pyproj) or pass --polygon already "
                "in the COPC CRS and omit --polygon-crs."
            )
        tf = Transformer.from_crs(src_crs, dst_crs, always_xy=True)

        def rp(ring):
            return [list(tf.transform(x, y)) for x, y in ring]
    else:
        def rp(ring):
            return [[x, y] for x, y in ring]

    if gtype == "Polygon":
        rings = [rp(r) for r in coords]
        return "POLYGON(" + ", ".join(_ring_wkt(r) for r in rings) + ")"
    if gtype == "MultiPolygon":
        polys = [[rp(r) for r in poly] for poly in coords]
        return "MULTIPOLYGON(" + ", ".join(
            "(" + ", ".join(_ring_wkt(r) for r in poly) + ")" for poly in polys
        ) + ")"
    sys.exit("Unsupported geometry type for crop: {}".format(gtype))


def main():
    ap = argparse.ArgumentParser(
        description="Range-read a spatial subset from a COPC (local, s3://, or https)."
    )
    ap.add_argument("--input", required=True,
                    help="COPC source: local path, s3://..., or https://...")
    ap.add_argument("--out", required=True,
                    help="Output file (.laz, .las, or .csv)")
    ap.add_argument("--bounds",
                    help='Crop bbox "minx,miny,maxx,maxy" in the COPC CRS')
    ap.add_argument("--polygon",
                    help="Path to a GeoJSON polygon/multipolygon to crop by")
    ap.add_argument("--polygon-crs", dest="polygon_crs",
                    help="CRS of the polygon (e.g. EPSG:4326); reprojected to --copc-crs")
    ap.add_argument("--copc-crs", dest="copc_crs",
                    help="Assert the COPC CRS (e.g. EPSG:27700); also the polygon reprojection target")
    ap.add_argument("--resolution", type=float,
                    help="Coarsest resolution to read in metres; skips fine LOD for fast broad passes")
    ap.add_argument("--stats", action="store_true",
                    help="Print point count / bounds / timing after the crop")
    args = ap.parse_args()

    if not args.bounds and not args.polygon and not args.resolution:
        ap.error("provide --bounds, --polygon, or --resolution")

    reader = {"type": "readers.copc", "filename": args.input}
    if args.copc_crs:
        reader["override_srs"] = args.copc_crs
    if args.resolution:
        reader["resolution"] = args.resolution

    if args.bounds:
        parts = args.bounds.split(",")
        if len(parts) != 4:
            ap.error('--bounds must be "minx,miny,maxx,maxy"')
        try:
            minx, miny, maxx, maxy = (float(v) for v in parts)
        except ValueError:
            ap.error("--bounds values must be numbers")
        reader["bounds"] = _bounds_to_pdal(minx, miny, maxx, maxy)

    if args.polygon:
        reader["polygon"] = _load_polygon_wkt(args.polygon, args.polygon_crs, args.copc_crs)

    ext = os.path.splitext(args.out)[1].lower()
    if ext in (".laz", ".las"):
        writer = {"type": "writers.las", "filename": args.out,
                  "compression": (ext == ".laz")}
    elif ext == ".csv":
        writer = {"type": "writers.text", "format": "csv",
                  "order": "X,Y,Z", "keep_unspecified": "true",
                  "filename": args.out}
    else:
        ap.error("--out must end in .laz, .las, or .csv")

    pipeline = {"pipeline": [reader, writer]}

    tf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    try:
        json.dump(pipeline, tf)
        tf.close()
        t0 = time.time()
        r = _run(["pdal", "pipeline", tf.name])
        dt = time.time() - t0
    finally:
        os.unlink(tf.name)

    if r.returncode != 0:
        sys.stderr.write(r.stderr or r.stdout or "")
        sys.exit("\npdal pipeline failed (exit {}).".format(r.returncode))

    if args.stats:
        n = None
        bnds = None
        info = _pdal_info_summary(args.out)
        if info:
            summ = info.get("summary", {})
            n = summ.get("num_points")
            bnds = summ.get("bounds")
        sz = os.path.getsize(args.out) if os.path.exists(args.out) else 0
        print("OK  {:.1f}s".format(dt))
        print("    points : {}".format(n if n is not None else "?"))
        print("    output : {}  ({:.1f} MB)".format(args.out, sz / 1e6))
        if bnds:
            print("    bounds : {}".format(bnds))


if __name__ == "__main__":
    main()