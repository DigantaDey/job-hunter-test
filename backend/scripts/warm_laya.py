"""Pre-download the Laya checkpoints Job Hunter routes to, so decisions work offline.

The app routes between the ``english`` and ``multilingual`` checkpoints only
(see ``app/services/laya.py``: no explicit ``task``/``model`` beyond
``multilingual`` for long documents, and ``auto_task_detection`` stays off).
``--all`` also fetches ``typed-decisions``, which Job Hunter never requests
today. Weights land in the Hugging Face cache (``$HF_HOME`` or
``~/.cache/huggingface``) and a marker file records the warmed set.

Run with the backend's Python environment (and runtime user):

    python backend/scripts/warm_laya.py              # warm, then write the marker
    python backend/scripts/warm_laya.py --if-needed  # skip when the marker covers the set (run.sh)
    python backend/scripts/warm_laya.py --check      # prove the cache is complete, network off (CI)

No application settings, database, credentials or external network are needed
once warm; a cold cache needs network access to Hugging Face. ``--check``
forces Hugging Face into offline mode first, so it can only pass when the
weights are truly present — that is what the Docker smoke test asserts with
``--network none``.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

#: Checkpoints this app can request (app/services/laya.py routes english vs
#: multilingual; typed-decisions needs an explicit task/model we never pass).
CHECKPOINTS = ["english", "multilingual"]
EXTRA_CHECKPOINTS = ["typed-decisions"]
MARKER_NAME = "jobhunter-laya-warm"


def marker_path() -> Path:
    """The warmed-set marker, pinned next to the Hugging Face cache root."""
    root = os.environ.get("HF_HOME")
    base = Path(root) if root else Path.home() / ".cache" / "huggingface"
    return base / MARKER_NAME


def warmed_set() -> List[str]:
    """Checkpoint names recorded by a previous warm-up (empty when cold)."""
    try:
        raw = marker_path().read_text(encoding="utf-8")
    except OSError:
        return []
    return [name for name in raw.replace("\n", "").split(",") if name]


def wanted(all_checkpoints: bool) -> List[str]:
    return CHECKPOINTS + (EXTRA_CHECKPOINTS if all_checkpoints else [])


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the warmed cache with Hugging Face forced offline (no marker write)",
    )
    parser.add_argument(
        "--if-needed",
        action="store_true",
        help="skip when the marker already covers the set (used by run.sh)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="also warm the typed-decisions checkpoint (unused by this app)",
    )
    args = parser.parse_args(argv)
    want = wanted(bool(args.all))

    done = warmed_set()
    if args.if_needed and all(name in done for name in want):
        print("Laya checkpoints already warmed (%s); skipping." % ", ".join(want))
        return 0

    if args.check and not all(name in done for name in want):
        print(
            "marker %s covers %s but %s requested — run warm_laya.py first."
            % (marker_path(), ", ".join(done) or "nothing", ", ".join(want)),
            file=sys.stderr,
        )
        return 1

    try:
        from laya import Router
    except ImportError:
        print(
            "laya is not installed (pip install -r backend/requirements-laya.txt); nothing to warm.",
            file=sys.stderr,
        )
        return 1

    if args.check:
        # Set before the first download call: a cache miss must fail loudly
        # instead of quietly fetching, or --check would pass on a cold cache.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"

    try:
        Router().preload(list(want))
    except Exception as exc:  # any loader failure means "not warm"
        print("Laya warm-up failed: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1

    if args.check:
        print("Laya checkpoints load offline: %s." % ", ".join(want))
        return 0

    marker_path().parent.mkdir(parents=True, exist_ok=True)
    marker_path().write_text(
        ",".join(sorted(set(done) | set(want))) + "\n", encoding="utf-8"
    )
    print("Laya warm: %s (marker %s)." % (", ".join(want), marker_path()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
