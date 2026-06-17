# Troubleshooting

## The install button does nothing / asks to pick an app

- The button opens a `vscode.dev` / `cursor.com` redirect that hands off to the desktop
  app. The app must be installed and have opened at least once. If your browser blocks the
  app handoff, copy the config from [`examples/`](../examples/) into the client manually.
- Buttons embed a repo URL and model. If you see `your-model-id` in the copied config,
  that's expected — replace it with the model ID your endpoint serves.

## `fastcontext: command not found` (or the tool errors with that)

The explorer CLI isn't on PATH. Install it once:

```bash
uv tool install git+https://github.com/microsoft/fastcontext
```

GUI editors sometimes don't inherit your shell PATH — restart the editor after installing.

## `uvx: command not found`

Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh`, then restart your shell or
editor so `uvx` is on PATH.

## First call is very slow, then fine

Expected. On first run `uvx` builds the server from git and caches it; later runs are
fast. A self-hosted model also loads slowly on its first request after boot.

## Every call errors with "no model configured"

You didn't pass `--model` in the server `args`. Add the model id your endpoint serves,
e.g. `--model qwen2.5-coder-7b`. The connection comes from `args`, not environment
variables — check the `args` in your client config or button.

## Calls hang or time out

- Confirm the endpoint is up and `--base-url` is right (LM Studio defaults to
  `http://localhost:1234/v1` and must be started from its Developer tab).
- Test it directly:
  `BASE_URL=... MODEL=... API_KEY=lm-studio fastcontext -q "list entry points" --max-turns 4`
- Lower `max_turns`, sharpen the query, or raise the server's `--timeout` (default 300s).

## The MCP tool never appears in the agent

- **VS Code:** the file is `.vscode/mcp.json`, top-level key `servers` (not `mcpServers`).
  Reload the window and check the MCP view for start errors.
- **Codex CLI:** the header is `[mcp_servers.<name>]` with an **underscore**.
- **Cursor / Claude Code / Cline:** top-level key `mcpServers`.
- Restart the agent after editing config — most clients read it at startup.

## Results are empty or off-target

FastContext returns nothing when the query is vague or the code isn't in the repo. Re-ask
with a sharper query naming the symbol, error, or subsystem. For monorepos, pass
`repo_path` explicitly.

## Same query finds the code sometimes but not others

FastContext samples at **temperature 1.0** (the explorer's exploration is intentionally
non-deterministic), so two identical calls can take different search paths and one may
miss what the other finds. This is expected, not a bug. The MCP server already retries
once with a larger turn budget on an empty result; beyond that, **simply calling again**
is often enough. A plain grep can also beat it on a literal token in an unusual file —
that's a known trade-off of a learned explorer, not a failure.

## The agent calls FastContext, then re-scans the repo anyway

It's missing the usage guidance. Add
[`prompts/fastcontext-usage.md`](../prompts/fastcontext-usage.md) to the agent's
instructions so it trusts the citations and reads narrowly.

## Is it safe?

FastContext is read-only (Read / Glob / Grep) and never modifies files. The MCP server
only shells out to it. Trajectory logs are written to a temp file, not your repo.
