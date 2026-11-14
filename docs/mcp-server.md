# Add an MCP server so agents can call pagemark as a tool

## Summary

Ship a Model Context Protocol (MCP) server so agents such as Claude Code, Claude Desktop, and Cursor can call pagemark as a tool. An agent could then turn a PDF into Markdown and get pagemark's per-page confidence and warnings, instead of using a plain text extractor that hides failures.

## Motivation

- Agents run into PDFs all the time (papers, specs, invoices). They usually fall back to `pdftotext`-style extraction, which drops tables and columns without saying so.
- pagemark's main feature is reporting failures: each `Page` has `confidence`, `warnings`, and `source` (`vlm` / `text_layer`). That is useful to an agent, which can re-check low-confidence pages or tell the user about them.
- Today an agent has to shell out to `pagemark convert` and parse files. A typed tool interface is cleaner and returns structured results.

## Proposed design

### Packaging

- New optional extra: `pagemark[mcp]`, depending on the standalone `fastmcp` package (Apache-2.0, so the license constraint is met). The floor is `fastmcp>=4.1`: 4.2 needs Python 3.11, and 4.1.x still supports 3.10.
- New module `src/pagemark/mcp_server.py`, built on `fastmcp.FastMCP`.
- Entry point: a `pagemark mcp` subcommand in `cli.py`, plus a `pagemark-mcp` console script so the server can run without the `cli` extra.
- Transport: stdio only in v1. Streamable HTTP can come later.

### Configuration (no hardcoded providers)

The endpoint config comes from the server launch, not from tool arguments, so API keys never pass through the model:

- `--base-url` / `PAGEMARK_BASE_URL` (required)
- `--model` / `PAGEMARK_MODEL` (default model; tools may override it)
- API key: `--api-key`, then `PAGEMARK_API_KEY`, then `OPENAI_API_KEY`, following the existing `resolve_api_key` order. Log only which source was used, never the value.
- `--root` (repeatable) / `PAGEMARK_ROOTS`: folders the tools may read and write. Default: the working directory.
- `--max-inline-chars`: larger results go to disk (default 100,000).

Example client config:

```json
{
  "mcpServers": {
    "pagemark": {
      "command": "uvx",
      "args": ["--from", "pagemark[mcp]", "pagemark-mcp"],
      "env": {
        "PAGEMARK_BASE_URL": "http://localhost:11434/v1",
        "PAGEMARK_MODEL": "qwen3-vl:8b"
      }
    }
  }
}
```

### Tools

Conversions are background jobs. `convert_pdf` returns a job id at once and the agent polls `job_status`, so a long PDF never runs into a client's tool-call timeout.

| tool | args | returns |
| --- | --- | --- |
| `convert_pdf` | `path`, `pages?` (e.g. `"1-3,7"`), `model?`, `profile?`, `max_tokens?`, `concurrency?`, `output_path?`, `assets_dir?` | `job_id`, `state: running`, `pages_total`. Refused while another job runs. |
| `job_status` | `job_id` | `state` (running / done / failed / cancelled), `pages_done`/`pages_total`, elapsed time, `error`. Once done: `low_confidence_pages` first (confidence < 0.7 or text-layer fallback), then the per-page summary (`page`, `confidence`, `source`, `warnings`), token usage, and the Markdown inline, or `markdown_path` when it was written to disk. |
| `cancel_job` | `job_id` | the job's status. Pages already sent to the model finish first. |
| `pdf_info` | `path` | page count and, per page, size, whether there is a text layer, and image count. No model call. Refused while a job runs. |
| `doctor` | none | the same endpoint/model probe as `pagemark doctor`, plus where the key came from. |

Possibly later: `convert_page` (one page as typed blocks), a resource interface (`pagemark://doc/{id}/page/{n}`) for reading large documents page by page, and the client-provided MCP roots as an alternative to `--root`.

### Job slot

- There is exactly one job slot, held in memory. A restart forgets it; the agent just converts again.
- While the job is running, `convert_pdf` and `pdf_info` are refused with an error naming the running job and telling the agent to poll or cancel it. `job_status`, `cancel_job`, and `doctor` always work.
- A finished job (done, failed, or cancelled) stays readable until the next `convert_pdf` replaces it; then the old id is reported as unknown.
- The slot is released whenever the job ends, including on failure or cancellation, and a rejected request (bad path, bad page range, unreadable PDF) never takes it.

### Behavior

- The job runs `aconvert()` on its own thread with its own event loop, so the server keeps answering pings and status calls while pages render.
- pdfium is not thread-safe, even across separate documents. The single-job rule covers this: outside the job thread, pdfium is only touched under the slot lock while no job is running.
- langchain-openai caches one async HTTP client per base URL for the whole process. Since each job gets a fresh event loop, each job also gets its own `httpx.AsyncClient`, closed when the job ends.
- Large outputs: if the Markdown exceeds `--max-inline-chars`, it is written to `<tempdir>/pagemark-mcp/<job_id>.md` and `job_status` returns the path. With `output_path`, it is written through `Document.save()` and never inlined.
- Paths: relative paths resolve against the first root; everything is resolved (following symlinks) and rejected if it lands outside every root. This covers `path`, `output_path`, and `assets_dir`.

### Constraints this must keep (from CLAUDE.md)

- No `print()`. On stdio, stdout carries the protocol, so every log line must go to stderr through the `pagemark` logger.
- No `logging.basicConfig()`. The server attaches its handler to the `pagemark` logger, as the CLI does.
- No provider defaults or named-provider constants.
- The `mcp` dependency stays optional, and the core install doesn't grow.

## Blocking render (resolved)

Rendering (`render_page`, `build_anchor`, `extract_images`) is synchronous. Running the whole conversion on the job thread keeps that off the server's event loop without changing `pipeline.py`. The pipeline itself still renders synchronously inside its own loop, so within one job the concurrency semaphore still limits model calls, not CPU work.

## Testing

- `tests/test_mcp_server.py`: unit tests with `FakeListChatModel` (and a gated variant that holds a job in `running`), driven through fastmcp's in-memory `Client`. Covers the job lifecycle, the busy lock, cancellation, slot replacement, low-confidence reporting, path rejection, output spilling, and config/key handling. No network needed.
- `tests/test_mcp_integration.py`: under `-m integration`, launches `pagemark-mcp` over stdio against `PAGEMARK_TEST_BASE_URL` / `PAGEMARK_TEST_MODEL`.

## Acceptance criteria

- [x] `pip install pagemark[mcp]` provides a working `pagemark-mcp` stdio server
- [x] `convert_pdf`, `job_status`, `cancel_job`, `pdf_info`, and `doctor` tools, with typed input and output schemas
- [x] Per-page confidence, source, and warnings returned with every conversion
- [x] Endpoint and key configured at launch only; key never logged or echoed
- [x] Nothing written to stdout except protocol messages
- [x] Unit tests with no network; ruff, mypy strict, and the existing suite pass
- [x] README section with config snippets for Claude Code and Claude Desktop
