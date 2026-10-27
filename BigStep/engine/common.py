"""Shared helpers for the BigStep engine processes.

Event protocol: every engine process writes one JSON object per line to its
*original* stdout.  OCCT prints its own messages to stdout as well, so
`open_event_channel()` duplicates the real stdout to a private fd and points
fd 1 at stderr; anything OCCT prints ends up in the log, never in the protocol.
"""
import json
import os
import sys
import time

_chan = None


def open_event_channel():
    global _chan
    if _chan is None:
        fd = os.dup(1)
        os.dup2(2, 1)
        sys.stdout = os.fdopen(os.dup(2), "w", buffering=1)
        _chan = os.fdopen(fd, "w", buffering=1)
    return _chan


def emit(ev, **fields):
    fields["ev"] = ev
    fields.setdefault("t", round(time.time(), 3))
    chan = _chan or sys.stdout
    chan.write(json.dumps(fields, ensure_ascii=False) + "\n")
    chan.flush()


def peak_rss_mb():
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def rss_mb(pid):
    """Current resident memory of another process (Linux), or None."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return None


def occt_version():
    try:
        import OCP
        return getattr(OCP, "__version__", "unknown")
    except ImportError:
        return None
