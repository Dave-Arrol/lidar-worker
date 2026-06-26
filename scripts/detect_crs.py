"""
detect_crs.py — print the horizontal EPSG of a point cloud's declared CRS.

The worker calls this once per analysis to thread the cloud's real CRS through
every georeferenced stage, instead of every stage assuming EPSG:27700. It prints
exactly one token to stdout (nothing else; diagnostics go to stderr):

  EPSG:<code>   the cloud's horizontal (projected) CRS, e.g. EPSG:27700
  GEOGRAPHIC    the CRS is geographic (lat/lon, degrees) — not usable as-is, the
                pipeline (CSF cloth resolution, areas, rasters) assumes metres
  NONE          no CRS declared, or no EPSG code resolvable from it

Compound CRS (e.g. "OSGB36 / British National Grid + ODN height") are reduced to
their horizontal component, which is what the 2D rasters and vector outputs need;
the vertical (height) component is irrelevant after normalisation.

Only the header is read (laspy.open), so this is cheap even on a 10 GB LAZ.
"""

import sys


def detect(path):
    try:
        import laspy
    except Exception as exc:
        print(f"detect_crs: laspy import failed: {exc}", file=sys.stderr)
        return "NONE"

    try:
        with laspy.open(path) as reader:
            crs = reader.header.parse_crs()
    except Exception as exc:
        print(f"detect_crs: could not read CRS from {path}: {exc}", file=sys.stderr)
        return "NONE"

    if crs is None:
        return "NONE"

    # Reduce a compound (horizontal + vertical) CRS to its horizontal part.
    horiz = crs
    try:
        if getattr(crs, "is_compound", False) and crs.sub_crs_list:
            horiz = crs.sub_crs_list[0]
    except Exception:
        horiz = crs

    # Geographic (degrees) cannot be ground-filtered or rasterised as metres.
    try:
        if getattr(horiz, "is_geographic", False):
            return "GEOGRAPHIC"
    except Exception:
        pass

    # Resolve to an EPSG code; fall back to NONE (worker then assumes 27700).
    try:
        epsg = horiz.to_epsg()
    except Exception as exc:
        print(f"detect_crs: to_epsg failed: {exc}", file=sys.stderr)
        epsg = None

    return f"EPSG:{epsg}" if epsg else "NONE"


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("NONE")
        return
    print(detect(path))


if __name__ == "__main__":
    main()
