"""Live-endpoint test: launches ``pagemark-mcp`` over stdio and converts a PDF.

Run with ``uv run pytest -m integration`` after setting:

- ``PAGEMARK_TEST_BASE_URL``  OpenAI-compatible endpoint
- ``PAGEMARK_TEST_MODEL``     a vision-capable model served there
- ``PAGEMARK_API_KEY`` or ``OPENAI_API_KEY``
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastmcp")

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

pytestmark = pytest.mark.integration

BASE_URL = os.environ.get("PAGEMARK_TEST_BASE_URL")
MODEL = os.environ.get("PAGEMARK_TEST_MODEL")


@pytest.mark.skipif(not (BASE_URL and MODEL), reason="PAGEMARK_TEST_BASE_URL/MODEL not set")
async def test_stdio_convert_end_to_end(tmp_path: Path, simple_pdf: Path) -> None:
    assert BASE_URL is not None and MODEL is not None
    shutil.copy(simple_pdf, tmp_path / "simple.pdf")
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "pagemark.mcp_server", "--root", str(tmp_path), "-q"],
        env={**os.environ, "PAGEMARK_BASE_URL": BASE_URL, "PAGEMARK_MODEL": MODEL},
    )
    async with Client(transport) as client:
        doctor = (await client.call_tool("doctor", {})).structured_content
        assert doctor is not None and doctor["reachable"], doctor

        started = (
            await client.call_tool("convert_pdf", {"path": "simple.pdf"})
        ).structured_content
        assert started is not None
        job_id = started["job_id"]

        for _ in range(600):
            status = (await client.call_tool("job_status", {"job_id": job_id})).structured_content
            assert status is not None
            if status["state"] != "running":
                break
            await asyncio.sleep(0.5)

    assert status["state"] == "done", status
    assert "hello world" in status["markdown"].lower()
    assert status["pages"][0]["source"] in {"vlm", "text_layer"}
