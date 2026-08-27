"""Test fixtures.

Tests never touch your real state directory. ``CONVENE_HOME`` is redirected to
a temp dir for the whole session, before ``convene.config`` is imported.
"""

from __future__ import annotations

import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="convene-tests-")
os.environ["CONVENE_HOME"] = _TMP


@pytest.fixture(scope="session", autouse=True)
def _isolated_home() -> str:
    return _TMP
