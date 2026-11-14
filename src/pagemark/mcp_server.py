"""MCP server exposing pagemark as agent tools.

Conversions run as a single background job. ``convert_pdf`` returns a job id
immediately; the agent polls ``job_status`` for progress and the result. Only
one job runs at a time: while it is running, every tool that opens a PDF
(``convert_pdf``, ``pdf_info``) is refused with a busy error. Job state lives
in memory only, so a server restart forgets it and the caller simply retries.

The single-job rule also keeps pdfium safe: pdfium is not thread-safe, and the
job runs on its own thread with its own event loop. Every pdfium call made
outside that thread happens under the slot lock while no job is running.

Requires the ``mcp`` extra: ``pip install 'pagemark[mcp]'``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError
except ImportError as _exc:
    raise SystemExit(
        "pagemark MCP server requires the [mcp] extra: pip install 'pagemark[mcp]'"
    ) from _exc

import httpx
from pydantic import BaseModel, Field

from pagemark import __version__
from pagemark.backends import build_chat_model, resolve_api_key
from pagemark.models import Document, PageSource
from pagemark.pipeline import aconvert, parse_page_spec
from pagemark.render import open_pdf

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)

_DEFAULT_MAX_INLINE_CHARS = 100_000
_LOW_CONFIDENCE = 0.7
_IMAGE_OBJ_TYPE = 2  # FPDF_PAGEOBJ_IMAGE
_DOCTOR_MODEL_LIMIT = 50


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class ServerConfig:
    """Launch-time configuration. Nothing here is settable from a tool call."""

    base_url: str | None
    model: str | None
    api_key: str | None = field(default=None, repr=False)
    api_key_source: str = "none"
    roots: tuple[Path, ...] = ()
    max_inline_chars: int = _DEFAULT_MAX_INLINE_CHARS
    chat_model: BaseChatModel | None = field(default=None, repr=False)
    """Injected model for tests; replaces ``base_url``/``api_key``."""

    def __post_init__(self) -> None:
        if (self.base_url is None) == (self.chat_model is None):
            raise ValueError("ServerConfig needs exactly one of base_url or chat_model")
        if not self.roots:
            raise ValueError("ServerConfig needs at least one allowed root")


# --------------------------------------------------------------------------- results


class JobState(str, Enum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PageSummary(BaseModel):
    page: int = Field(description="1-based page number in the source PDF")
    confidence: float
    source: PageSource
    warnings: list[str]


class TokenTotals(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ConvertStarted(BaseModel):
    job_id: str
    state: JobState
    pages_total: int
    message: str


class JobStatus(BaseModel):
    job_id: str
    state: JobState
    path: str
    pages_done: int
    pages_total: int
    elapsed_seconds: float
    error: str | None = None
    low_confidence_pages: list[PageSummary] | None = Field(
        default=None,
        description=(
            f"Pages with confidence below {_LOW_CONFIDENCE} or recovered from the text "
            "layer. Check these before trusting the output."
        ),
    )
    pages: list[PageSummary] | None = None
    usage: TokenTotals | None = None
    markdown_chars: int | None = None
    markdown_path: str | None = Field(
        default=None,
        description="Where the Markdown was written, when it is not returned inline.",
    )
    markdown: str | None = None


class PageInfo(BaseModel):
    page: int
    width_pt: float
    height_pt: float
    has_text_layer: bool
    text_chars: int
    image_count: int


class PdfInfo(BaseModel):
    path: str
    page_count: int
    pages: list[PageInfo]


class DoctorReport(BaseModel):
    base_url: str | None
    api_key_source: str
    reachable: bool
    model: str | None
    model_available: bool | None = None
    models: list[str] = Field(default_factory=list)
    error: str | None = None


# --------------------------------------------------------------------------- job slot


@dataclass
class _Job:
    id: str
    path: str
    pages_total: int
    output_path: Path | None
    assets_dir: Path | None
    state: JobState = JobState.RUNNING
    pages_done: int = 0
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    error: str | None = None
    document: Document | None = None
    markdown_path: Path | None = None
    cancel_requested: bool = False
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task[Document] | None = None


class _Busy(Exception):
    def __init__(self, job: _Job) -> None:
        super().__init__(job.id)
        self.job = job


class JobSlot:
    """Holds at most one job. A running job blocks every PDF-opening tool."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._job: _Job | None = None

    def running_job(self) -> _Job | None:
        """Caller must hold ``lock``."""
        if self._job is not None and self._job.state is JobState.RUNNING:
            return self._job
        return None

    def install(self, job: _Job) -> None:
        """Replace the previous job. Caller must hold ``lock`` and have checked busy."""
        self._job = job

    def get(self, job_id: str) -> _Job | None:
        with self.lock:
            if self._job is not None and self._job.id == job_id:
                return self._job
            return None


# --------------------------------------------------------------------------- server


def build_server(config: ServerConfig) -> FastMCP:
    """Create the FastMCP server. Separate from :func:`serve` so tests can drive it."""
    slot = JobSlot()
    server = FastMCP(
        "pagemark",
        instructions=(
            "Convert PDFs to Markdown with a vision-language model. convert_pdf starts a "
            "background job and returns a job_id; poll job_status(job_id) until state is "
            "done, failed or cancelled. Only one conversion runs at a time. Every page "
            "carries a confidence score and warnings; check low_confidence_pages before "
            "relying on the text."
        ),
        version=__version__,
    )

    def busy_error(job: _Job) -> ToolError:
        return ToolError(
            f"pagemark is busy with job {job.id} ({job.pages_done}/{job.pages_total} pages "
            f"done). Poll job_status('{job.id}') until it finishes, or call "
            f"cancel_job('{job.id}'), then retry."
        )

    @server.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    def convert_pdf(
        path: str,
        pages: str | None = None,
        model: str | None = None,
        profile: str | None = None,
        max_tokens: int = 2048,
        concurrency: int | None = None,
        output_path: str | None = None,
        assets_dir: str | None = None,
    ) -> ConvertStarted:
        """Start converting a PDF to Markdown in the background and return a job id.

        Poll job_status(job_id) for progress and the result. Refused while another
        conversion is running.

        Args:
            path: PDF file, inside one of the server's allowed roots.
            pages: 1-based page selection such as "1-3,7" or "5-". Default: all pages.
            model: Override the server's default model.
            profile: Model profile name. Default: "generic".
            max_tokens: Max output tokens per page.
            concurrency: Pages converted in parallel within this job. Default: 4.
            output_path: Write the Markdown here instead of returning it inline.
            assets_dir: Write embedded images here (only used with output_path).
        """
        pdf_path = _resolve_inside(path, config.roots, must_exist=True)
        out = _resolve_inside(output_path, config.roots) if output_path else None
        assets = _resolve_inside(assets_dir, config.roots) if assets_dir else None
        model_id = model or config.model
        if config.chat_model is None and model_id is None:
            raise ToolError("No model given and the server was started without --model.")

        with slot.lock:
            current = slot.running_job()
            if current is not None:
                raise busy_error(current)
            pages_total = _count_selected_pages(pdf_path, pages)
            job = _Job(
                id=str(uuid.uuid4()),
                path=str(pdf_path),
                pages_total=pages_total,
                output_path=out,
                assets_dir=assets,
            )
            slot.install(job)
            thread = threading.Thread(
                target=_run_job,
                args=(slot, job, config),
                kwargs={
                    "model_id": model_id,
                    "pages": pages,
                    "profile": profile,
                    "max_tokens": max_tokens,
                    "concurrency": concurrency,
                },
                name=f"pagemark-job-{job.id[:8]}",
                daemon=True,
            )
            thread.start()

        logger.info("Started job %s for %s (%d pages)", job.id, pdf_path, pages_total)
        return ConvertStarted(
            job_id=job.id,
            state=JobState.RUNNING,
            pages_total=pages_total,
            message=(
                f"Conversion started. Poll job_status('{job.id}') until state is done, "
                "failed or cancelled."
            ),
        )

    @server.tool(annotations={"readOnlyHint": True})
    def job_status(job_id: str) -> JobStatus:
        """Report progress of a conversion job, and its result once it is done.

        Low-confidence pages are listed first. The Markdown is returned inline unless
        it is large or output_path was given, in which case markdown_path says where
        it was written.
        """
        job = slot.get(job_id)
        if job is None:
            raise ToolError(
                f"Unknown job id {job_id!r}. The server keeps only the most recent job in "
                "memory; it may have been replaced or the server restarted. Start a new "
                "conversion with convert_pdf."
            )
        with slot.lock:
            return _status_of(job, config.max_inline_chars)

    @server.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def cancel_job(job_id: str) -> JobStatus:
        """Cancel a running conversion job. Pages already in flight finish first."""
        job = slot.get(job_id)
        if job is None:
            raise ToolError(f"Unknown job id {job_id!r}.")
        with slot.lock:
            if job.state is JobState.RUNNING:
                job.cancel_requested = True
                if job.loop is not None and job.task is not None:
                    job.loop.call_soon_threadsafe(job.task.cancel)
                logger.info("Cancellation requested for job %s", job.id)
            return _status_of(job, config.max_inline_chars)

    @server.tool(annotations={"readOnlyHint": True})
    def pdf_info(path: str) -> PdfInfo:
        """Inspect a PDF without calling the model: page sizes, text layer, image count.

        Cheap; use it to decide which pages to convert. Refused while a conversion runs.
        """
        pdf_path = _resolve_inside(path, config.roots, must_exist=True)
        with slot.lock:
            current = slot.running_job()
            if current is not None:
                raise busy_error(current)
            return _inspect_pdf(pdf_path)

    @server.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
    async def doctor() -> DoctorReport:
        """Check that the configured endpoint is reachable and serves the default model."""
        return await _probe_endpoint(config)

    return server


# --------------------------------------------------------------------------- job runner


def _run_job(
    slot: JobSlot,
    job: _Job,
    config: ServerConfig,
    *,
    model_id: str | None,
    pages: str | None,
    profile: str | None,
    max_tokens: int,
    concurrency: int | None,
) -> None:
    """Thread target: run one conversion on a private event loop."""
    loop = asyncio.new_event_loop()
    try:
        doc = loop.run_until_complete(
            _convert(slot, job, config, loop, model_id, pages, profile, max_tokens, concurrency)
        )
        markdown_path = _write_outputs(job, doc, config.max_inline_chars)
        with slot.lock:
            job.document = doc
            job.markdown_path = markdown_path
            job.state = JobState.DONE
        logger.info("Job %s done", job.id)
    except asyncio.CancelledError:
        with slot.lock:
            job.state = JobState.CANCELLED
        logger.info("Job %s cancelled", job.id)
    except Exception as e:
        logger.warning("Job %s failed", job.id, exc_info=True)
        with slot.lock:
            job.error = f"{type(e).__name__}: {e}"
            job.state = JobState.FAILED
    finally:
        with slot.lock:
            job.finished_at = time.monotonic()
            job.loop = None
            job.task = None
        loop.close()


async def _convert(
    slot: JobSlot,
    job: _Job,
    config: ServerConfig,
    loop: asyncio.AbstractEventLoop,
    model_id: str | None,
    pages: str | None,
    profile: str | None,
    max_tokens: int,
    concurrency: int | None,
) -> Document:
    # langchain-openai caches one async httpx client per base_url for the whole
    # process. Each job runs on a fresh event loop, so a shared client would hold
    # connections bound to a closed loop. Give every job its own client.
    http_client: httpx.AsyncClient | None = None
    if config.chat_model is not None:
        chat_model = config.chat_model
        model_name = model_id or getattr(chat_model, "model_name", None) or "custom"
    else:
        assert config.base_url is not None and model_id is not None
        http_client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
        chat_model = build_chat_model(
            model_id,
            base_url=config.base_url,
            api_key=config.api_key,
            max_tokens=max_tokens,
            http_async_client=http_client,
        )
        model_name = model_id

    def on_progress(page_index: int, total_pages: int, status: str) -> None:
        if status == "completed":
            with slot.lock:
                job.pages_done += 1

    try:
        task = loop.create_task(
            aconvert(
                job.path,
                chat_model=chat_model,
                model_id=model_name,
                profile=profile,
                pages=pages,
                concurrency=concurrency,
                progress=on_progress,
            )
        )
        with slot.lock:
            job.loop = loop
            job.task = task
            if job.cancel_requested:
                task.cancel()
        return await task
    finally:
        if http_client is not None:
            await http_client.aclose()


def _write_outputs(job: _Job, doc: Document, max_inline_chars: int) -> Path | None:
    if job.output_path is not None:
        doc.save(job.output_path, assets_dir=job.assets_dir)
        return job.output_path
    if len(doc.markdown) > max_inline_chars:
        out_dir = Path(tempfile.gettempdir()) / "pagemark-mcp"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{job.id}.md"
        doc.save(path)
        return path
    return None


def _status_of(job: _Job, max_inline_chars: int) -> JobStatus:
    """Caller must hold the slot lock."""
    end = job.finished_at if job.finished_at is not None else time.monotonic()
    status = JobStatus(
        job_id=job.id,
        state=job.state,
        path=job.path,
        pages_done=job.pages_done,
        pages_total=job.pages_total,
        elapsed_seconds=round(end - job.started_at, 1),
        error=job.error,
    )
    doc = job.document
    if job.state is not JobState.DONE or doc is None:
        return status

    summaries = [
        PageSummary(
            page=p.index + 1, confidence=p.confidence, source=p.source, warnings=p.warnings
        )
        for p in doc.pages
    ]
    status.low_confidence_pages = [
        s for s in summaries if s.confidence < _LOW_CONFIDENCE or s.source is PageSource.TEXT_LAYER
    ]
    status.pages = summaries
    totals = TokenTotals()
    for p in doc.pages:
        if p.usage is not None:
            totals.prompt_tokens += p.usage.prompt_tokens
            totals.completion_tokens += p.usage.completion_tokens
            totals.total_tokens += p.usage.total_tokens
    status.usage = totals
    status.markdown_chars = len(doc.markdown)
    if job.markdown_path is not None:
        status.markdown_path = str(job.markdown_path)
    elif len(doc.markdown) <= max_inline_chars:
        status.markdown = doc.markdown
    return status


# --------------------------------------------------------------------------- helpers


def _resolve_inside(raw: str, roots: Sequence[Path], *, must_exist: bool = False) -> Path:
    """Resolve ``raw`` and require it to sit inside one of ``roots``.

    Relative paths resolve against the first root. Symlinks are resolved first, so
    a link inside a root that points outside it is rejected.
    """
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = roots[0] / candidate
    resolved = candidate.resolve()
    if not any(resolved.is_relative_to(root) for root in roots):
        allowed = ", ".join(str(r) for r in roots)
        raise ToolError(f"Path {raw!r} is outside the allowed roots ({allowed}).")
    if must_exist and not resolved.is_file():
        raise ToolError(f"File not found: {raw!r}")
    return resolved


def _count_selected_pages(pdf_path: Path, pages: str | None) -> int:
    """Open the PDF once to validate it and the page spec. Caller holds the slot lock."""
    try:
        pdf = open_pdf(pdf_path)
    except Exception as e:
        raise ToolError(f"Could not open {str(pdf_path)!r} as a PDF: {e}") from e
    try:
        return len(parse_page_spec(pages, len(pdf)))
    except ValueError as e:
        raise ToolError(f"Invalid pages {pages!r}: {e}") from e
    finally:
        pdf.close()


def _inspect_pdf(pdf_path: Path) -> PdfInfo:
    """Caller holds the slot lock with no job running (pdfium is not thread-safe)."""
    try:
        pdf = open_pdf(pdf_path)
    except Exception as e:
        raise ToolError(f"Could not open {str(pdf_path)!r} as a PDF: {e}") from e
    try:
        infos: list[PageInfo] = []
        for i in range(len(pdf)):
            # Close children before the document; pypdfium2 warns otherwise.
            page = pdf[i]
            try:
                try:
                    textpage = page.get_textpage()
                    try:
                        text: str = textpage.get_text_range().strip()
                    finally:
                        textpage.close()
                except Exception:
                    text = ""
                try:
                    images = sum(1 for obj in page.get_objects() if obj.type == _IMAGE_OBJ_TYPE)
                except Exception:
                    images = 0
                infos.append(
                    PageInfo(
                        page=i + 1,
                        width_pt=round(page.get_width(), 1),
                        height_pt=round(page.get_height(), 1),
                        has_text_layer=bool(text),
                        text_chars=len(text),
                        image_count=images,
                    )
                )
            finally:
                page.close()
        return PdfInfo(path=str(pdf_path), page_count=len(infos), pages=infos)
    finally:
        pdf.close()


async def _probe_endpoint(config: ServerConfig) -> DoctorReport:
    report = DoctorReport(
        base_url=config.base_url,
        api_key_source=config.api_key_source,
        reachable=False,
        model=config.model,
    )
    if config.base_url is None:
        report.reachable = True
        report.error = "server is using an injected chat model; nothing to probe"
        return report

    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{config.base_url.rstrip('/')}/models", headers=headers)
            r.raise_for_status()
            data: Any = r.json()
    except Exception as e:
        report.error = f"{type(e).__name__}: {e}"
        return report

    entries = data.get("data", []) if isinstance(data, dict) else []
    available = [str(m.get("id", "?")) for m in entries if isinstance(m, dict)]
    report.reachable = True
    report.models = available[:_DOCTOR_MODEL_LIMIT]
    if config.model is not None:
        report.model_available = config.model in available
    return report


# --------------------------------------------------------------------------- entry points


def make_config(
    *,
    base_url: str | None,
    model: str | None,
    api_key: str | None,
    roots: Sequence[str] | None,
    max_inline_chars: int | None = None,
) -> ServerConfig:
    """Build a :class:`ServerConfig` from flags, falling back to environment variables.

    Precedence: flag, then ``PAGEMARK_*`` env var. The API key additionally falls back
    to ``OPENAI_API_KEY`` (via :func:`resolve_api_key`). Roots default to the current
    working directory.
    """
    base_url = base_url or os.environ.get("PAGEMARK_BASE_URL")
    if not base_url:
        raise ValueError("--base-url or PAGEMARK_BASE_URL is required")
    model = model or os.environ.get("PAGEMARK_MODEL")

    if api_key is not None:
        source = "--api-key"
    elif os.environ.get("PAGEMARK_API_KEY"):
        api_key = os.environ["PAGEMARK_API_KEY"]
        source = "PAGEMARK_API_KEY"
    else:
        api_key = resolve_api_key(base_url, None)
        source = "OPENAI_API_KEY"
    logger.info("API key resolved from %s", source)

    if not roots:
        env_roots = os.environ.get("PAGEMARK_ROOTS")
        roots = env_roots.split(os.pathsep) if env_roots else [os.getcwd()]
    resolved_roots = tuple(Path(r).expanduser().resolve() for r in roots if r)

    return ServerConfig(
        base_url=base_url,
        model=model,
        api_key=api_key,
        api_key_source=source,
        roots=resolved_roots,
        max_inline_chars=max_inline_chars or _DEFAULT_MAX_INLINE_CHARS,
    )


def configure_logging(verbose: bool = False, quiet: bool = False) -> None:
    """Attach a stderr handler to the ``pagemark`` logger. stdout carries the protocol."""
    level = logging.DEBUG if verbose else logging.ERROR if quiet else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s: %(message)s"))
    pkg_logger = logging.getLogger("pagemark")
    pkg_logger.setLevel(level)
    pkg_logger.addHandler(handler)


def serve(config: ServerConfig) -> None:
    """Run the server over stdio until the client disconnects."""
    logger.info(
        "pagemark MCP server %s starting (base_url=%s, model=%s, roots=%s)",
        __version__,
        config.base_url,
        config.model,
        ", ".join(str(r) for r in config.roots),
    )
    build_server(config).run(transport="stdio", show_banner=False)


def main(argv: Sequence[str] | None = None) -> None:
    """``pagemark-mcp`` console script."""
    parser = argparse.ArgumentParser(
        prog="pagemark-mcp", description="pagemark MCP server (stdio)."
    )
    parser.add_argument("--base-url", help="OpenAI-compatible base URL [PAGEMARK_BASE_URL]")
    parser.add_argument("--model", help="Default model [PAGEMARK_MODEL]")
    parser.add_argument(
        "--api-key", help="API key [PAGEMARK_API_KEY, then OPENAI_API_KEY]; prefer env vars"
    )
    parser.add_argument(
        "--root",
        action="append",
        dest="roots",
        help="Directory the server may read and write; repeatable "
        "[PAGEMARK_ROOTS, default: current directory]",
    )
    parser.add_argument(
        "--max-inline-chars",
        type=int,
        help=f"Larger results are written to disk (default {_DEFAULT_MAX_INLINE_CHARS})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="Errors only")
    args = parser.parse_args(argv)

    configure_logging(args.verbose, args.quiet)
    try:
        config = make_config(
            base_url=args.base_url,
            model=args.model,
            api_key=args.api_key,
            roots=args.roots,
            max_inline_chars=args.max_inline_chars,
        )
    except ValueError as e:
        parser.error(str(e))
    serve(config)


if __name__ == "__main__":
    main()
