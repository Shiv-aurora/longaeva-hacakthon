"""Gate for the few tests that read preserved local research caches.

Git tracks only the small declared inputs under ``data/`` (see ``.gitignore`` and
``data/README.md``). Raw source bytes and generated research stores stay local,
so a fresh clone or a CI checkout cannot run a test that opens them. Such a test
asks for its input through ``require_local_cache``: it is skipped, with a reason
naming the absent path and the document describing its rebuild, only when that
path does not exist. Nothing is skipped where the cache is present, and a cache
that is present but incomplete or altered still fails.

Set ``UAL_REQUIRE_LOCAL_CACHES=1`` on a machine that is expected to hold the full
local store: an absent cache then fails instead of skipping.
"""
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STRICT = "UAL_REQUIRE_LOCAL_CACHES"


def _require_local_cache(relative_path, doc, what="local research cache"):
    path = ROOT / relative_path
    if path.exists():
        return path
    reason = (f"Missing {what}: {relative_path} is not tracked in Git and is absent from this checkout. "
              f"Rebuild or transfer it as described in {doc} (Git boundary: data/README.md).")
    if os.environ.get(STRICT) == "1":
        pytest.fail(reason + f" {STRICT}=1 turns this skip into a failure.", pytrace=False)
    pytest.skip(reason)


@pytest.fixture(scope="session")
def require_local_cache():
    """Return ``require(relative_path, doc, what=...)``: the path, or skip when absent."""
    return _require_local_cache
