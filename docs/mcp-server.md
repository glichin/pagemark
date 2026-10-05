# Add an MCP server so agents can call pagemark as a tool

## Summary

Ship a Model Context Protocol (MCP) server so agents such as Claude Code, Claude Desktop, and Cursor can call pagemark as a tool. An agent could then turn a PDF into Markdown and get pagemark's per-page confidence and warnings, instead of using a plain text extractor that hides failures.

## Motivation

- Agents run into PDFs all the time (papers, specs, invoices). They usually fall back to `pdftotext`-style extraction, which drops tables and columns without saying so.
- pagemark's main feature is reporting failures: each `Page` has `confidence`, `warnings`, and `source` (`vlm` / `text_layer`). That is useful to an agent, which can re-check low-confidence pages or tell the user about them.
- Today an agent has to shell out to `pagemark convert` and parse files. A typed tool interface is cleaner and returns structured results.

## Proposed design

### Packaging

- New optional extra: `pagemark[mcp]`, depending on the official `mcp` Python SDK (MIT, so the license constraint is met).
- New module `src/pagemark/mcp_server.py`, built on `FastMCP`.
- Entry point: a `pagemark mcp` subcommand in `cli.py`, plus a `pagemark-mcp` console script so the server can run without the `cli` extra.
- Transport: stdio by default. Streamable HTTP is optional (`--transport http --port ...`).

### Configuration (no hardcoded providers)

The endpoint config comes from the server launch, not from tool arguments, so API keys never pass through the model:

- `--base-url` / `PAGEMARK_BASE_URL` (required)
- `--model` / `PAGEMARK_MODEL` (default model; tools may override it)
- API key: `--api-key`, then `PAGEMARK_API_KEY`, then `OPENAI_API_KEY`, following the existing `resolve_api_key` order. Log only which source was used, never the value.

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

| tool | args | returns |
| --- | --- | --- |
| `convert_pdf` | `path`, `pages?` (e.g. `"1-3,7"`), `model?`, `profile?`, `max_tokens?`, `concurrency?`, `output_path?`, `assets_dir?` | stitched Markdown, plus a per-page summary (`index`, `confidence`, `source`, `warnings`) and token usage. If `output_path` is given, it writes through `Document.save()` and returns the path and summary instead of the full text. |
| `pdf_info` | `path` | page count, whether each page has a text layer, and embedded image count. No model call, so it's cheap to run before converting. |
| `doctor` | none | the same endpoint/model probe as `pagemark doctor`. |

Possibly later: `convert_page` (one page returned as typed blocks), and the converted document exposed as an MCP resource (`pagemark://doc/{id}/page/{n}`) so large documents can be read page by page instead of in one huge tool result.

### Behavior

- Use `aconvert()` directly. The MCP server already runs an event loop, so `convert()` and its `asyncio.run` must not be called there.
- Report progress through MCP progress notifications using the existing `ProgressCallback`.
- Put low-confidence and text-layer-fallback pages near the top of the result (for example, a `warnings` section before the Markdown) so the agent sees them.
- Large outputs: if the Markdown exceeds a configurable size, return a summary and write the full output to disk, or point to the resource interface.
- Paths: resolve against an allow-list of roots (`--root`, or the client-provided MCP roots) and reject anything outside them.

### Constraints this must keep (from CLAUDE.md)

- No `print()`. On stdio, stdout carries the protocol, so every log line must go to stderr through the `pagemark` logger.
- No `logging.basicConfig()`. The server attaches its handler to the `pagemark` logger, as the CLI does.
- No provider defaults or named-provider constants.
- The `mcp` dependency stays optional, and the core install doesn't grow.

## Known issue to handle

Rendering (`render_page`, `build_anchor`, `extract_images`) is synchronous and blocks the event loop. In a CLI that doesn't matter much. In a long-running server, it stalls MCP pings and progress messages while a large PDF converts. Two options: run each conversion through `asyncio.to_thread`, keeping one `PdfDocument` per conversion, or accept the blocking for v1 and document it.

## Testing

- Unit tests with `FakeListChatModel`: build the server with an injected `chat_model` and call the tools through the SDK's in-memory client session. No network needed.
- Check path allow-list rejection, the large-output fallback, and that warnings and confidence appear in the result.
- An integration test under `-m integration` that launches `pagemark-mcp` over stdio against a live endpoint.

## Acceptance criteria

- [ ] `pip install pagemark[mcp]` provides a working `pagemark-mcp` stdio server
- [ ] `convert_pdf`, `pdf_info`, and `doctor` tools, with typed input schemas
- [ ] Per-page confidence, source, and warnings returned with every conversion
- [ ] Endpoint and key configured at launch only; key never logged or echoed
- [ ] Nothing written to stdout except protocol messages
- [ ] Unit tests with no network; ruff, mypy strict, and the existing suite pass
- [ ] README section with config snippets for Claude Code and Claude Desktop
