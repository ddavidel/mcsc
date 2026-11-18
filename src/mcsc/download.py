"""HTTP download helper."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import requests

from mcsc.exceptions import DownloadError

logger = logging.getLogger(__name__)


def download_file(url: str, destination: Path, *, timeout: float = 30) -> Path:
    """Stream ``url`` to ``destination``.

    Writes to a temporary file first, so a failed download never leaves a
    truncated file at ``destination``.
    """
    destination = Path(destination)
    tmp = destination.with_name(destination.name + ".part")
    try:
        with requests.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            with open(tmp, "wb") as file:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    file.write(chunk)
        os.replace(tmp, destination)
    except requests.RequestException as e:
        tmp.unlink(missing_ok=True)
        raise DownloadError(f"Error downloading {url}: {e}") from e
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    logger.info("Downloaded %s to %s", url, destination)
    return destination
