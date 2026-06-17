#!/usr/bin/env python3
"""Generate one-click MCP install buttons + per-client config snippets for this server.

The deeplink/badge formats are baked in from each vendor's docs (verified 2026-06-17).
Because the buttons embed YOUR GitHub repo URL and model, regenerate them after you
publish the repo:

    python scripts/make-install-buttons.py \
        --repo LIVELUCKY/fastcontext-integrations \
        --model qwen2.5-coder-7b

Then paste the printed Markdown into README.md. Add --write-examples examples/ to also
refresh the copy-paste config files in examples/.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from urllib.parse import quote


def git_url(repo: str) -> str:
    if repo.startswith(("git+", "http://", "https://")):
        return repo if repo.startswith("git+") else f"git+{repo}"
    return f"git+https://github.com/{repo}"


def build(args) -> dict:
    gurl = git_url(args.repo)
    run_args = [
        "--from", gurl, "fastcontext-mcp",
        "--base-url", args.base_url,
        "--model", args.model,
        "--api-key", args.api_key,
    ]
    name = args.name

    # Per-client server objects
    cursor_obj = {"command": "uvx", "args": run_args}
    vscode_obj = {"type": "stdio", "command": "uvx", "args": run_args}

    # Cursor web installer: base64(JSON), URL-encoded into the query.
    cursor_b64 = base64.b64encode(json.dumps(cursor_obj, separators=(",", ":")).encode()).decode()
    cursor_url = f"https://cursor.com/install-mcp?name={quote(name)}&config={quote(cursor_b64, safe='')}"

    # VS Code redirect installer: URL-encoded JSON.
    vscode_enc = quote(json.dumps(vscode_obj, separators=(",", ":")), safe="")
    vscode_url = f"https://vscode.dev/redirect/mcp/install?name={quote(name)}&config={vscode_enc}"
    insiders_url = f"https://insiders.vscode.dev/redirect/mcp/install?name={quote(name)}&config={vscode_enc}&quality=insiders"

    claude_cmd = (
        f"claude mcp add {name} -- uvx --from {gurl} fastcontext-mcp "
        f"--base-url {args.base_url} --model {args.model} --api-key {args.api_key}"
    )

    return {
        "gurl": gurl, "name": name, "run_args": run_args,
        "cursor_url": cursor_url, "vscode_url": vscode_url, "insiders_url": insiders_url,
        "claude_cmd": claude_cmd,
    }


def badges_md(b: dict) -> str:
    return "\n".join([
        f"[![Add to Cursor](https://img.shields.io/badge/Add_to-Cursor-000000?style=flat-square&logo=cursor&logoColor=white)]({b['cursor_url']})",
        f"[![Install in VS Code](https://img.shields.io/badge/VS_Code-Install-0098FF?style=flat-square&logo=visualstudiocode&logoColor=white)]({b['vscode_url']})",
        f"[![Install in VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Install-24bfa5?style=flat-square&logo=visualstudiocode&logoColor=white)]({b['insiders_url']})",
    ])


def snippets_md(b: dict) -> str:
    a = json.dumps(b["run_args"])
    claude_json = json.dumps({"mcpServers": {b["name"]: {"command": "uvx", "args": b["run_args"]}}}, indent=2)
    return f"""### Claude Code

```bash
{b['claude_cmd']}
```

### Codex CLI — add to `~/.codex/config.toml`

```toml
[mcp_servers.{b['name']}]
command = "uvx"
args = {a}
enabled = true
```

### Manual JSON (Cursor `.cursor/mcp.json`, Claude `.mcp.json`, Cline settings)

```json
{claude_json}
```
"""


def write_examples(b: dict, out: str) -> None:
    os.makedirs(out, exist_ok=True)
    name, run_args = b["name"], b["run_args"]
    # mcpServers-style (Cursor, Claude Code)
    mcp_servers = {"mcpServers": {name: {"command": "uvx", "args": run_args}}}
    with open(os.path.join(out, "cursor.mcp.json"), "w") as f:
        json.dump(mcp_servers, f, indent=2); f.write("\n")
    with open(os.path.join(out, "claude-code.mcp.json"), "w") as f:
        json.dump(mcp_servers, f, indent=2); f.write("\n")
    # VS Code: servers + type
    with open(os.path.join(out, "vscode.mcp.json"), "w") as f:
        json.dump({"servers": {name: {"type": "stdio", "command": "uvx", "args": run_args}}}, f, indent=2); f.write("\n")
    # Windsurf: mcpServers (same format as Cursor/Claude Code)
    with open(os.path.join(out, "windsurf.mcp.json"), "w") as f:
        json.dump(mcp_servers, f, indent=2); f.write("\n")
    # Cline: mcpServers + disabled/autoApprove
    with open(os.path.join(out, "cline.mcp.json"), "w") as f:
        json.dump({"mcpServers": {name: {"command": "uvx", "args": run_args,
                                         "disabled": False, "autoApprove": ["fastcontext_explore"]}}},
                  f, indent=2); f.write("\n")
    # Codex TOML
    with open(os.path.join(out, "codex.config.toml"), "w") as f:
        f.write(f"[mcp_servers.{name}]\ncommand = \"uvx\"\nargs = {json.dumps(run_args)}\nenabled = true\n")
    print(f"Wrote example configs to {out}/")


def main() -> None:
    p = argparse.ArgumentParser(description="Generate FastContext MCP install buttons + snippets")
    p.add_argument("--repo", default="LIVELUCKY/fastcontext-integrations", help="GitHub owner/repo or a git+https URL")
    p.add_argument("--model", default="your-model-id", help="Model id your endpoint serves")
    p.add_argument("--base-url", default="http://localhost:1234/v1", help="OpenAI-compatible base URL")
    p.add_argument("--api-key", default="lm-studio", help="API key (ignored by local servers)")
    p.add_argument("--name", default="fastcontext", help="Server name shown in the client")
    p.add_argument("--write-examples", metavar="DIR", help="Also (re)write copy-paste config files here")
    args = p.parse_args()

    b = build(args)
    print("<!-- install buttons (regenerate with scripts/make-install-buttons.py) -->")
    print(badges_md(b))
    print()
    print(snippets_md(b))
    if args.write_examples:
        write_examples(b, args.write_examples)


if __name__ == "__main__":
    main()
