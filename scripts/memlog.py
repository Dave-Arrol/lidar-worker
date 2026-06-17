"""
memlog.py  -  one-line peak-memory reporting for the analysis stages.

Each stage runs as its own `python3` process, so the process peak RSS isolates
that stage, while the cgroup peak is the whole container's high-water mark - the
number Fargate's OOM-killer actually compares against the task's memory limit.
Both print to stdout, which lands in the worker's CloudWatch logs.

Usage - add ONE line right after a stage's imports:

    import memlog
    memlog.track("segmentation")     # or "normalise", "dbh", ...

On exit the stage prints, e.g.:

    [mem] segmentation: process peak RSS=2.31 GiB | container cgroup peak=2.44 GiB

Reading the numbers:
  * process peak RSS   -> that stage's own peak (resets per stage process)
  * container cgroup   -> cumulative across the whole task so far, so the LARGEST
                          "container cgroup peak" line across the run is the run's
                          true peak (usually printed by the heaviest stage).
"""
import atexit
import resource

_GIB = 1024.0 ** 3


def _read_cgroup_peak():
    """Return the container's peak memory in bytes (cgroup v2 then v1), or None."""
    for path in ("/sys/fs/cgroup/memory.peak",                      # cgroup v2
                 "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"):  # cgroup v1
        try:
            with open(path) as f:
                return int(f.read().strip())
        except Exception:
            continue
    return None


def report(tag):
    # ru_maxrss is KiB on Linux
    proc_gib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0 / 1024.0
    msg = f"[mem] {tag}: process peak RSS={proc_gib:.2f} GiB"
    cg = _read_cgroup_peak()
    if cg is not None:
        msg += f" | container cgroup peak={cg / _GIB:.2f} GiB"
    print(msg, flush=True)


def track(tag):
    """Register report(tag) to fire when this stage's process exits."""
    atexit.register(report, tag)
