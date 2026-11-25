import argparse
import logging
import os
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx

from api_side.incidents import database, destination_key
from api_side.telegram import deliver_pending, telegram_config

logger = logging.getLogger(__name__)
PROFILES = (
    "az-nextcloud.ovpn",
    "az-gerasimov.ovpn",
    "az-naebalovanet.ovpn",
    "az-gen1.ovpn",
)


def pending() -> bool:
    config = telegram_config()
    if config is None:
        return False
    destination = destination_key(*config)
    now = time.time()
    with database() as db:
        return db.execute(
            """
            SELECT 1 FROM incidents
            WHERE destination = ? AND delivered_revision < revision AND next_attempt <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM delivery_cooldowns WHERE destination = ? AND until > ?
              )
            LIMIT 1
            """,
            (destination, now, destination, now),
        ).fetchone() is not None


def telegram_reachable() -> bool:
    try:
        response = httpx.get("https://api.telegram.org/", timeout=4)
        return response.status_code < 500
    except httpx.HTTPError:
        return False


@contextmanager
def tunnel(profile: Path) -> Iterator[bool]:
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(
            ["openvpn", "--config", str(profile), "--redirect-gateway", "def1"],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 24
            while time.monotonic() < deadline and process.poll() is None:
                if b"Initialization Sequence Completed" in os.pread(
                    log.fileno(), os.fstat(log.fileno()).st_size, 0
                ):
                    yield True
                    return
                time.sleep(0.2)
            yield False
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def run(probe: bool) -> int:
    os.umask(0o007)
    if not probe and not pending():
        return 0
    for name in PROFILES:
        profile = Path("/app/data") / name
        if not profile.is_file():
            logger.warning("AZ profile missing: %s", name)
            continue
        with tunnel(profile) as ready:
            if ready and telegram_reachable():
                logger.info("Telegram reachable through %s", name)
                if not probe:
                    deliver_pending()
                return 0
        logger.warning("Telegram unavailable through %s", name)
    return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    raise SystemExit(run(parser.parse_args().probe))
