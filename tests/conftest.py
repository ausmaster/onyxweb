"""Shared test fixtures.

These tests require a usable Chromium binary. onyxweb auto-resolves from:
  1. explicit chrome_path= on Client (not used here)
  2. bundled python/onyxweb/_binaries/<platform>/chrome-headless-shell
  3. system chromium (apt install chromium-browser etc.)

If neither bundled nor system chromium is available, tests that spin a Client
will fail at Client() construction with a clear "chrome binary not found"
error — intended. Install chromium to run the suite.
"""

from __future__ import annotations

import base64
from collections.abc import Callable

import pytest


@pytest.fixture
def data_url() -> Callable[[bytes], str]:
    """Wrap HTML bytes in a base64-encoded ``data:`` URL.

    Tests using ``data:`` URLs avoid the cost of spinning up an HTTP
    server when the test only needs a tiny HTML document loaded once.
    """

    def _make(html: bytes) -> str:
        return "data:text/html;base64," + base64.b64encode(html).decode()

    return _make
