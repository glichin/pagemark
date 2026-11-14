# pagemark

[![PyPI](https://img.shields.io/pypi/v/pagemark)](https://pypi.org/project/pagemark/)
[![Python](https://img.shields.io/pypi/pyversions/pagemark)](https://pypi.org/project/pagemark/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/glichin/pagemark/blob/main/LICENSE)

pagemark converts PDFs to Markdown with vision-language models.

Use it with any OpenAI-compatible endpoint. You provide the `base_url`, `model`,
and `api_key`. pagemark renders each page, grounds the model with the PDF text
layer, and checks the result for common failures: repetition loops, truncation,
dropped columns, and hallucinated tables. If a check fails, it retries with a
stricter strategy and falls back to the text layer if the model still cannot
recover.

If one page fails, the document still finishes. pagemark records warnings for
that page instead of aborting the whole conversion.

## Prerequisites

- Python 3.10+
- An OpenAI-compatible endpoint serving a vision-language model
  (Ollama, vLLM, LM Studio, any hosted provider, etc.)

## Install

```bash
pip install pagemark            # library only
pip install "pagemark[cli]"     # with CLI
pip install "pagemark[mcp]"     # with the MCP server
```

With [uv](https://docs.astral.sh/uv/):

```bash
uv add pagemark                 # add to your project
uv add "pagemark[cli]"          # with CLI
uv add "pagemark[mcp]"          # with the MCP server

uv tool install "pagemark[cli]" # standalone CLI, no project needed
uvx --from "pagemark[cli]" pagemark doctor --help  # run it without installing
uvx --from "pagemark[mcp]" pagemark-mcp --help     # MCP server, same idea
```

The `--from` is needed for the `uvx` forms: plain `uvx pagemark` installs the
package without the `cli` or `mcp` extra, and the command exits telling you
which extra to add.

## Quick start

```python
from pagemark import Pagemark

client = Pagemark(
    base_url="http://localhost:11434/v1", # OpenAI-compatible API
    api_key="your-key", # API key
)

doc = client.convert("paper.pdf", model="qwen3-vl:8b")

print(doc.markdown)
doc.save("out.md", assets_dir="out_assets/")
```

`api_key` is optional. pagemark resolves it from the explicit argument first,
then from `OPENAI_API_KEY`. If neither exists, it raises an error that names the
`base_url`.

### Async

`aconvert()` is the implementation behind `convert()`. `convert()` wraps it with
`asyncio.run()`. Pages run concurrently, bounded by a semaphore. The default
limit is 4.

```python
from pagemark import Pagemark

client = Pagemark(
    base_url="http://localhost:11434/v1", # OpenAI-compatible API
    api_key="your-key", # API key
)

doc = await client.aconvert(
    "paper.pdf",
    model="qwen3-vl:8b",
    concurrency=8,
)
```

### Page selection

Convert selected pages instead of the whole document:

```python
doc = client.convert("paper.pdf", model="qwen3-vl:8b", pages="1-5,9,12-")
```

`pages` accepts `"3"`, `"1-5"`, `"1-5,9,12-"`, or a `list[int]`. Out-of-range
indices raise an error instead of being silently clipped.

### Bring your own model

If you need settings the constructor does not expose, pass a pre-configured
`BaseChatModel`. That covers custom timeouts, proxies, non-OpenAI LangChain
backends like `ChatAnthropic` or `ChatOllama`, callbacks, and similar options.

```python
from langchain_openai import ChatOpenAI
from pagemark import Pagemark

my_model = ChatOpenAI(
    model="...",
    base_url="...",
    api_key="...",
    timeout=120,
    max_retries=5,
)

client = Pagemark(chat_model=my_model)
doc = client.convert("paper.pdf")
```

### CLI

```bash
pagemark convert paper.pdf -o paper.md \
    --model qwen3-vl:8b \
    --base-url http://localhost:11434/v1

pagemark convert paper.pdf -o paper.md --json paper.json \
    --model qwen3-vl:8b \
    --base-url http://localhost:11434/v1

pagemark doctor --base-url http://localhost:11434/v1 --model qwen3-vl:8b
```

`doctor` checks whether the endpoint is reachable, whether the model is present,
and where the API key came from.

### MCP server

`pagemark[mcp]` adds an [MCP](https://modelcontextprotocol.io) server, so agents
such as Claude Code, Claude Desktop, and Cursor can call pagemark as a tool and
see each page's confidence and warnings instead of silently trusting the text.
It runs over stdio and is started with `pagemark-mcp` (or `pagemark mcp` if the
CLI extra is installed too).

The endpoint, model, and key are fixed when the server starts, so the key never
passes through the agent:

| flag | env var | |
| --- | --- | --- |
| `--base-url` | `PAGEMARK_BASE_URL` | required |
| `--model` | `PAGEMARK_MODEL` | default model; `convert_pdf` can override it |
| `--api-key` | `PAGEMARK_API_KEY`, then `OPENAI_API_KEY` | prefer the env vars |
| `--root` (repeatable) | `PAGEMARK_ROOTS` (path-separator list) | folders the server may read and write; default is the working directory |

Claude Code:

```bash
claude mcp add pagemark \
    --env PAGEMARK_BASE_URL=http://localhost:11434/v1 \
    --env PAGEMARK_MODEL=qwen3-vl:8b \
    --env PAGEMARK_API_KEY=unused \
    -- uvx --from "pagemark[mcp]" pagemark-mcp --root ~/Documents
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "pagemark": {
      "command": "uvx",
      "args": ["--from", "pagemark[mcp]", "pagemark-mcp", "--root", "/Users/me/Documents"],
      "env": {
        "PAGEMARK_BASE_URL": "http://localhost:11434/v1",
        "PAGEMARK_MODEL": "qwen3-vl:8b",
        "PAGEMARK_API_KEY": "unused"
      }
    }
  }
}
```

Desktop clients often launch servers from a directory you didn't choose, so
pass `--root` explicitly there. Local servers that ignore the key still need
some value, because pagemark refuses to start without one.

Tools:

| tool | what it does |
| --- | --- |
| `convert_pdf` | starts a conversion in the background and returns a `job_id` right away |
| `job_status` | progress (`pages_done`/`pages_total`), then the result: low-confidence pages first, a per-page summary, token usage, and the Markdown |
| `cancel_job` | cancels the running job |
| `pdf_info` | page sizes, text layer, and image count, without calling the model |
| `doctor` | checks the endpoint is reachable and serves the default model |

The server runs **one conversion at a time**. While a job is running,
`convert_pdf` and `pdf_info` are refused with an error naming the running job;
`job_status`, `cancel_job`, and `doctor` keep working. The job and its result
live in memory until the next `convert_pdf` replaces them, so a restarted
server has forgotten them and the agent just converts again. Markdown larger
than 100,000 characters (`--max-inline-chars`), or any result with
`output_path` set, is written to disk and `job_status` returns the path instead.

## How it works

```
pdf -> render + anchor -> prompt (profile) -> ChatOpenAI -> guardrails -> parse -> assemble -> Document
       pypdfium2          per-model           langchain      retry ladder   md-it    stitching   md + json
```

1. Render each page to a PNG with pypdfium2, downscaled to the profile's pixel
   budget.
2. Extract the PDF text layer as an anchor for the model. Long pages are
   middle-elided to about 6k characters.
3. Send the image and anchor text to the VLM.
4. Check every response with five guardrails.
5. Retry with stricter strategies when a guardrail fails.
6. Assemble the pages into a `Document` with typed blocks, then stitch across
   pages by stripping running headers and footers, joining split paragraphs, and
   dehyphenating text.

### Guardrails

| Check | What it catches |
|---|---|
| Repetition | n-gram loops, collapsed gzip ratio |
| Truncation | `finish_reason == "length"` |
| Coverage | anchor tokens missing from output; catches silently dropped columns |
| Hallucination | high rate of output tokens absent from anchor (born-digital pages) |
| Refusal | "I'm sorry", "I cannot", near-empty output on a non-blank page |

### Escalation ladder

When any guardrail fails, pagemark retries with more aggressive strategies:

| Step | Strategy | Confidence ceiling |
|---|---|---|
| 1 | Normal render, standard prompt | 0.95 |
| 2 | Reseed: `temperature=0.3`, `seed=42` | 0.85 |
| 3 | High DPI: DPI x1.5 (capped at 400), pixels x1.5 | 0.75 |
| 4 | Fallback prompt: simpler instruction | 0.60 |
| 5 | Text layer fallback | 0.30 |

Each page includes `source`, `confidence`, and `warnings`, so downstream
consumers can filter by quality.

## Output

`convert()` returns a `Document` with:

- `markdown`: the full stitched Markdown output
- `pages`: list of `Page` objects, each with:
  - `markdown`: per-page Markdown
  - `blocks`: typed blocks (heading, paragraph, table, figure, code, list, formula, caption, footnote)
  - `source`: `"vlm"` or `"text_layer"` (if the model couldn't handle the page)
  - `confidence`: 0-1 score
  - `warnings`: what went wrong, if anything
- `assets`: embedded images extracted from the PDF
- `model_dump_json()`: full structured output as JSON
- `save(path, assets_dir=...)`: writes the Markdown file and extracted images

## License

MIT. Runtime dependencies, including the `cli` and `mcp` extras, are
permissively licensed - mostly MIT/Apache-2.0/BSD,
with a few under MPL-2.0 (certifi, orjson) and PSF-2.0 (typing-extensions). No
GPL/LGPL/AGPL dependencies in the tree.
