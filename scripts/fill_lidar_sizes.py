#!/usr/bin/env python3
"""
fill_lidar_sizes.py — populate lidar_jobs.size_bytes from S3 object metadata.

For each lidar_jobs row with size_bytes NULL, HEADs its raw/octree/copc keys
in the arrol-lidar bucket (metadata only — nothing is downloaded) and writes
the summed bytes back. Safe to re-run any time; only fills NULLs.

Run where AWS creds for arrol-lidar exist (Dave's machine or the worker):
    pip install boto3 supabase
    set SUPABASE_URL=https://pgmunrskmbjtqtnltvhn.supabase.co
    set SUPABASE_SERVICE_ROLE_KEY=...
    python fill_lidar_sizes.py            # fill missing
    python fill_lidar_sizes.py --dry-run  # show what it would do
"""
import os, sys
from pathlib import Path

def load_env_local():
    """Load .env.local (worker convention) into the environment — checked in
    the current directory, then the script's parent. Existing env wins."""
    for d in (Path.cwd(), Path(__file__).resolve().parent.parent):
        f = d / ".env.local"
        if not f.is_file():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)
        print(f"loaded {f}")
        return
    print("no .env.local found (using existing environment)")

load_env_local()

import boto3
from supabase import create_client

BUCKET = "arrol-lidar"
REGION = os.environ.get("AWS_REGION", "eu-west-2")

def key_from_path(p: str | None) -> str | None:
    """Accept raw keys, s3://bucket/key, or full URLs; return the bare key."""
    if not p:
        return None
    p = p.strip()
    if p.startswith("s3://"):
        parts = p[5:].split("/", 1)
        return parts[1] if len(parts) == 2 else None
    if "amazonaws.com/" in p:
        return p.split("amazonaws.com/", 1)[1]
    return p.lstrip("/") or None

def main():
    dry = "--dry-run" in sys.argv
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    s3 = boto3.client("s3", region_name=REGION)

    rows = sb.table("lidar_jobs").select("id, raw_path, octree_path, copc_path") \
             .is_("size_bytes", "null").execute().data
    print(f"{len(rows)} lidar_jobs rows without size_bytes")

    filled = missing = 0
    for r in rows:
        total = 0
        found_any = False
        for col in ("raw_path", "octree_path", "copc_path"):
            key = key_from_path(r.get(col))
            if not key:
                continue
            try:
                h = s3.head_object(Bucket=BUCKET, Key=key)
                total += h["ContentLength"]
                found_any = True
            except s3.exceptions.ClientError:
                pass  # key gone / renamed — skip, count what exists
        if not found_any:
            missing += 1
            continue
        if dry:
            print(f"  {r['id']}: {total/1e6:.1f} MB (would write)")
        else:
            sb.table("lidar_jobs").update({"size_bytes": total}).eq("id", r["id"]).execute()
        filled += 1

    print(f"done: {filled} filled{' (dry-run)' if dry else ''}, {missing} had no reachable keys")
    print("Worker TODO: set size_bytes on upload + stamp started_at/completed_at "
          "around processing, so this script never needs running again.")

if __name__ == "__main__":
    main()