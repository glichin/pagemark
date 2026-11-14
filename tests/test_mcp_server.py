from __future__ import annotations

import asyncio
import logging
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult

pytest.importorskip("fastmcp")

from fastmcp import Client
from fastmcp.exceptions import ToolError

from pagemark.anchor import build_anchor
from pagemark.mcp_server import ServerConfig, build_server, make_config
from pagemark.render import open_pdf

GOOD_PAGE = "# Hello World\n\nThis is a test document."


class GatedChatModel(FakeListChatModel):
    """Fake model whose calls block until ``gate`` is set (from any thread)."""

    gate: Any = None

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        while not self.gate.is_set():
            await asyncio.sleep(0.01)
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


@pytest.fixture
def root(tmp_path: Path, simple_pdf: Path, multipage_pdf: Path) -> Path:
    shutil.copy(simple_pdf, tmp_path / "simple.pdf")
    shutil.copy(multipage_pdf, tmp_path / "multi.pdf")
    return tmp_path


def _config(root: Path, model: FakeListChatModel, **kw: Any) -> ServerConfig:
    return ServerConfig(base_url=None, model=None, chat_model=model, roots=(root,), **kw)


async def _call(client: Client, tool: str, **args: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, args)
    assert result.structured_content is not None
    return result.structured_content


async def _wait_finished(client: Client, job_id: str, timeout: float = 15.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        status = await _call(client, "job_status", job_id=job_id)
        if status["state"] != "running":
            return status
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"job still running: {status}")
        await asyncio.sleep(0.02)


async def test_lists_tools(root: Path) -> None:
    server = build_server(_config(root, FakeListChatModel(responses=[GOOD_PAGE])))
    async with Client(server) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {"convert_pdf", "job_status", "cancel_job", "pdf_info", "doctor"}


async def test_convert_returns_job_then_result(root: Path) -> None:
    # Echo the text layer so the coverage guardrail scores the page as trustworthy.
    faithful = build_anchor(open_pdf(root / "simple.pdf"), 0)
    server = build_server(_config(root, FakeListChatModel(responses=[faithful])))
    async with Client(server) as client:
        started = await _call(client, "convert_pdf", path="simple.pdf")
        assert started["state"] == "running"
        assert started["pages_total"] == 1
        status = await _wait_finished(client, started["job_id"])

    assert status["state"] == "done"
    assert status["pages_done"] == 1
    assert "Hello World" in status["markdown"]
    assert status["low_confidence_pages"] == []
    [page] = status["pages"]
    assert page["page"] == 1
    assert page["source"] == "vlm"
    assert page["confidence"] > 0.7


async def test_busy_while_running_and_slot_replaced_after(root: Path) -> None:
    gate = threading.Event()
    model = GatedChatModel(responses=[GOOD_PAGE], gate=gate)
    server = build_server(_config(root, model))
    async with Client(server) as client:
        first = await _call(client, "convert_pdf", path="simple.pdf")
        job_id = first["job_id"]

        with pytest.raises(ToolError, match=f"busy with job {job_id}"):
            await client.call_tool("convert_pdf", {"path": "multi.pdf"})
        with pytest.raises(ToolError, match="busy"):
            await client.call_tool("pdf_info", {"path": "simple.pdf"})

        running = await _call(client, "job_status", job_id=job_id)
        assert running["state"] == "running"
        assert running["markdown"] is None
        doctor = await _call(client, "doctor")  # never blocked by the lock
        assert doctor["reachable"] is True

        gate.set()
        assert (await _wait_finished(client, job_id))["state"] == "done"

        info = await _call(client, "pdf_info", path="multi.pdf")
        assert info["page_count"] == 5

        second = await _call(client, "convert_pdf", path="simple.pdf")
        assert second["job_id"] != job_id
        with pytest.raises(ToolError, match="Unknown job id"):
            await client.call_tool("job_status", {"job_id": job_id})
        await _wait_finished(client, second["job_id"])


async def test_cancel_releases_the_slot(root: Path) -> None:
    gate = threading.Event()
    server = build_server(_config(root, GatedChatModel(responses=[GOOD_PAGE], gate=gate)))
    async with Client(server) as client:
        job_id = (await _call(client, "convert_pdf", path="multi.pdf"))["job_id"]
        await _call(client, "cancel_job", job_id=job_id)
        status = await _wait_finished(client, job_id)
        assert status["state"] == "cancelled"
        assert status["markdown"] is None

        gate.set()
        again = await _call(client, "convert_pdf", path="simple.pdf")
        assert (await _wait_finished(client, again["job_id"]))["state"] == "done"


async def test_low_confidence_pages_listed(root: Path) -> None:
    # Every attempt is a refusal, so the page exhausts the ladder and falls back.
    server = build_server(_config(root, FakeListChatModel(responses=["Sorry."])))
    async with Client(server) as client:
        job_id = (await _call(client, "convert_pdf", path="simple.pdf"))["job_id"]
        status = await _wait_finished(client, job_id)

    assert status["state"] == "done"
    [low] = status["low_confidence_pages"]
    assert low["source"] == "text_layer"
    assert "fell back to text layer" in low["warnings"]


@pytest.mark.parametrize("bad", ["../outside.pdf", "/etc/passwd", "C:/Windows/win.ini"])
async def test_rejects_paths_outside_roots(root: Path, bad: str) -> None:
    server = build_server(_config(root, FakeListChatModel(responses=[GOOD_PAGE])))
    async with Client(server) as client:
        with pytest.raises(ToolError, match="outside the allowed roots"):
            await client.call_tool("convert_pdf", {"path": bad})
        with pytest.raises(ToolError, match="outside the allowed roots"):
            await client.call_tool("pdf_info", {"path": bad})
        with pytest.raises(ToolError, match="outside the allowed roots"):
            await client.call_tool(
                "convert_pdf", {"path": "simple.pdf", "output_path": "../out.md"}
            )


async def test_missing_file_and_bad_page_spec(root: Path) -> None:
    server = build_server(_config(root, FakeListChatModel(responses=[GOOD_PAGE])))
    async with Client(server) as client:
        with pytest.raises(ToolError, match="File not found"):
            await client.call_tool("convert_pdf", {"path": "nope.pdf"})
        with pytest.raises(ToolError, match="Invalid pages"):
            await client.call_tool("convert_pdf", {"path": "simple.pdf", "pages": "4-9"})
        # a rejected request must not leave the slot busy
        job_id = (await _call(client, "convert_pdf", path="simple.pdf"))["job_id"]
        await _wait_finished(client, job_id)


async def test_output_path_written_not_inlined(root: Path) -> None:
    server = build_server(_config(root, FakeListChatModel(responses=[GOOD_PAGE])))
    async with Client(server) as client:
        started = await _call(client, "convert_pdf", path="simple.pdf", output_path="out/doc.md")
        job_id = started["job_id"]
        status = await _wait_finished(client, job_id)

    out = root / "out" / "doc.md"
    assert status["markdown"] is None
    assert Path(status["markdown_path"]) == out
    assert "Hello World" in out.read_text(encoding="utf-8")


async def test_large_output_spills_to_disk(root: Path) -> None:
    config = _config(root, FakeListChatModel(responses=[GOOD_PAGE]), max_inline_chars=10)
    async with Client(build_server(config)) as client:
        job_id = (await _call(client, "convert_pdf", path="simple.pdf"))["job_id"]
        status = await _wait_finished(client, job_id)

    assert status["markdown"] is None
    assert status["markdown_chars"] > 10
    spilled = Path(status["markdown_path"])
    try:
        assert "Hello World" in spilled.read_text(encoding="utf-8")
    finally:
        spilled.unlink()


async def test_pdf_info_reports_text_layer(root: Path) -> None:
    server = build_server(_config(root, FakeListChatModel(responses=[GOOD_PAGE])))
    async with Client(server) as client:
        info = await _call(client, "pdf_info", path="simple.pdf")
    [page] = info["pages"]
    assert page["has_text_layer"] is True
    assert page["text_chars"] > 0
    assert page["image_count"] == 0


def test_make_config_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("PAGEMARK_BASE_URL", "http://env.invalid/v1")
    monkeypatch.setenv("PAGEMARK_MODEL", "env-model")
    monkeypatch.setenv("PAGEMARK_API_KEY", "sk-pagemark-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
    caplog.set_level(logging.DEBUG, logger="pagemark")

    cfg = make_config(base_url=None, model="flag-model", api_key=None, roots=[str(tmp_path)])

    assert cfg.base_url == "http://env.invalid/v1"
    assert cfg.model == "flag-model"
    assert cfg.api_key == "sk-pagemark-secret"
    assert cfg.api_key_source == "PAGEMARK_API_KEY"
    assert cfg.roots == (tmp_path.resolve(),)
    assert "secret" not in caplog.text
    assert "secret" not in repr(cfg)

    monkeypatch.delenv("PAGEMARK_API_KEY")
    assert make_config(base_url=None, model=None, api_key=None, roots=None).api_key_source == (
        "OPENAI_API_KEY"
    )


def test_make_config_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAGEMARK_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="base-url"):
        make_config(base_url=None, model=None, api_key="k", roots=None)


async def test_failed_job_reports_error_and_releases_slot(root: Path) -> None:
    (root / "taken").mkdir()  # saving Markdown onto a directory fails after conversion
    server = build_server(_config(root, FakeListChatModel(responses=[GOOD_PAGE])))
    async with Client(server) as client:
        started = await _call(client, "convert_pdf", path="simple.pdf", output_path="taken")
        status = await _wait_finished(client, started["job_id"])
        assert status["state"] == "failed"
        assert status["error"]
        assert status["markdown"] is None

        again = await _call(client, "convert_pdf", path="simple.pdf")
        assert (await _wait_finished(client, again["job_id"]))["state"] == "done"
