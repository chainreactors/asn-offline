"""Daily refresh sidecar for the asn_offline container.

Wraps ``workflow.jobs.asn_refresh.refresh_all`` (copied into the container as
``/app/refresh.py``) under an apscheduler cron trigger. On a successful run,
the new dataset is built into a date-stamped subdirectory and the
``dataset.current`` symlink under ``$ASN_VOLUME_ROOT`` is atomically swapped.
The FastAPI lookup process detects the changed symlink / manifest and reloads
the in-memory singleton on the next request.

Configuration (env):
    ASN_VOLUME_ROOT     Volume root. Default /var/lib/asn_offline.
    ASN_REFRESH_CRON    apscheduler crontab string. Default "0 3 * * *" (UTC).

Notes:
    - The first refresh is run by entrypoint.sh *before* this sidecar starts
      (synchronous bootstrap), so this loop only handles steady-state cron.
    - We do not call ``get_dataset(reload=True)`` from here — the FastAPI
      app compares ``dataset.current`` / ``MANIFEST.json`` identity on each
      request and reloads lazily when it changes. Lookups that land between
      rename(2) and the next pyasn re-read will still get the previous
      dataset, which is acceptable.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

sys.path.insert(0, "/app")
import refresh as refresh_mod  # type: ignore[import-not-found]

logger = logging.getLogger("asn_offline.refresh_sidecar")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


def _atomic_swap_current(versioned: Path, current_link: Path) -> None:
    """Repoint ``current_link`` at ``versioned`` atomically.

    Linux ``rename(2)`` over an existing symlink is atomic; concurrent
    readers traversing the symlink see either the old or new target,
    never neither.
    """
    tmp_link = current_link.with_suffix(current_link.suffix + ".tmp")
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    tmp_link.symlink_to(versioned, target_is_directory=True)
    os.replace(tmp_link, current_link)


def _new_versioned_dir(volume_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"dataset.{stamp}.{os.getpid()}"
    for attempt in range(100):
        suffix = "" if attempt == 0 else f".{attempt}"
        candidate = volume_root / f"{base}{suffix}"
        try:
            candidate.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError(f"could not allocate versioned dataset dir under {volume_root}")


def run_refresh() -> None:
    volume_root = Path(os.environ.get("ASN_VOLUME_ROOT", "/var/lib/asn_offline")).resolve()
    volume_root.mkdir(parents=True, exist_ok=True)

    versioned = _new_versioned_dir(volume_root)

    logger.info("starting refresh into %s", versioned)
    state = refresh_mod.refresh_all(data_dir=versioned)
    if state.failures and not state.sources:
        logger.error("refresh produced no sources, skipping swap: %s", state.failures)
        return

    current = volume_root / "dataset.current"
    _atomic_swap_current(versioned, current)
    logger.info(
        "refresh complete, sources=%d failures=%d, current -> %s",
        len(state.sources),
        len(state.failures),
        versioned.name,
    )


def main() -> int:
    cron = os.environ.get("ASN_REFRESH_CRON", "0 3 * * *")
    try:
        trigger = CronTrigger.from_crontab(cron, timezone="UTC")
    except Exception as exc:
        logger.error(
            "invalid ASN_REFRESH_CRON=%r: %s — falling back to '0 3 * * *'",
            cron, exc,
        )
        trigger = CronTrigger.from_crontab("0 3 * * *", timezone="UTC")

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(run_refresh, trigger, id="asn_refresh", max_instances=1, coalesce=True)
    logger.info("refresh sidecar scheduled, cron=%r (UTC)", cron)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("refresh sidecar shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
