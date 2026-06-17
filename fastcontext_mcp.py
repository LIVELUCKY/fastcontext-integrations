#!/usr/bin/env python3
"""FastContext MCP server — zero dependencies, connection configured via MCP args.

Exposes microsoft/fastcontext as one MCP tool, `fastcontext_explore`, so any MCP-capable
coding agent (Claude Code, GitHub Copilot, OpenAI Codex CLI, Cursor, Cline, ...) can
delegate read-only repository exploration and get back compact `file:line` citations.

Two deliberate design choices:

1. **Zero dependencies.** Only the Python standard library (>=3.10). It speaks the small
   slice of MCP it needs by hand (newline-delimited JSON-RPC 2.0 over stdio). No pip install.

2. **Connection comes from MCP server args, not OS environment variables.** You pass the
   endpoint in the MCP client config's `args` array:

       python fastcontext_mcp.py --base-url http://localhost:1234/v1 --model your-model --api-key lm-studio

   The server then injects those into the `fastcontext` subprocess environment when it
   shells out to the CLI. (The upstream CLI reads BASE_URL/MODEL/API_KEY from env; that is
   now an internal implementation detail you never manage in your shell.)

Point `--base-url` at whatever you already run: LM Studio (default below), an
Unsloth-finetuned model served behind an OpenAI-compatible runtime, Ollama, a gateway, or
a hosted API. Anything that speaks OpenAI `/v1/chat/completions`.

Prerequisite (once): install the explorer CLI on PATH:
    uv tool install git+https://github.com/microsoft/fastcontext

Run it however your MCP client launches a stdio server — path-independent via uv:
    uvx --from git+https://github.com/LIVELUCKY/fastcontext-integrations fastcontext-mcp --model <loaded-model-id>
or directly:
    python fastcontext_mcp.py --model <loaded-model-id>

Debug logs go to stderr ONLY; stdout carries protocol traffic and nothing else.
See docs/SETUP.md and docs/TROUBLESHOOTING.md.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import platform
import re
import shutil
import subprocess
import sys

SERVER_NAME = "fastcontext"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL = "2025-06-18"

# Populated from CLI args in main(). These are the MCP "variables" that replace env vars.
CONFIG = {
    "base_url": "http://localhost:1234/v1",  # LM Studio default
    "model": None,
    "api_key": "lm-studio",  # local servers ignore this; any non-empty string works
    "repo": None,            # default exploration root; None -> the server's cwd
    "default_max_turns": 8,
    "max_escalate_turns": 14,  # upper turn budget for the deepest parallel explorer
    "parallel_attempts": 3,    # concurrent explorations; results are validated and unioned
    "timeout": 300,
    "max_chars": 20000,
}

TOOL = {
    "name": "fastcontext_explore",
    "description": (
        "Find where code lives in this repo. Delegates read-only exploration to FastContext "
        "and returns verified `path:line` citations. Call it before editing/reviewing/"
        "debugging when unsure where the relevant code is; then open only the cited ranges."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "One specific request naming the behavior, symbol, error, or file. "
                    "Specific beats vague (e.g. 'where webhook signatures are verified')."
                ),
            },
            "repo_path": {
                "type": "string",
                "description": "Repo or monorepo-subfolder to explore. Defaults to the server's repo/cwd.",
            },
            "max_turns": {
                "type": "integer",
                "description": "Optional turn budget. Omit to auto-tune; set higher for deep cross-file traces.",
            },
            "citation_only": {
                "type": "boolean",
                "description": "Default true (citations only). False adds a short explanation.",
            },
        },
        "required": ["query"],
    },
}


def log(msg: str) -> None:
    print(f"[fastcontext-mcp] {msg}", file=sys.stderr, flush=True)


# A cited path with optional :line or :line-range, e.g. core/EnvConfig.kt:91 or app.ts:10-25.
_CITED_PATH_RE = re.compile(r"(/?[\w.\-/]+\.[A-Za-z][A-Za-z0-9]*)(:\d+(?:-\d+)?)?")


def _resolve_cited_path(path: str, repo: str) -> str | None:
    """Resolve a cited path to a real file under `repo`, or None if it doesn't exist.

    Handles three cases generically (no hardcoded names):
      1. a correct absolute path,
      2. a path relative to the repo,
      3. a wrong-root absolute path (model hallucinated the leading prefix, e.g.
         /proj/app/foo.py instead of /home/user/proj/app/foo.py) — recovered by matching
         the longest path suffix that actually exists under repo.
    A path that resolves to nothing is a hallucination and the caller drops it.
    """
    if os.path.isabs(path) and os.path.isfile(path):
        return os.path.abspath(path)
    rel = os.path.join(repo, path.lstrip("/"))
    if os.path.isfile(rel):
        return os.path.abspath(rel)
    parts = [p for p in path.split("/") if p]
    for i in range(1, len(parts)):  # strip leading dirs until a real suffix appears
        cand = os.path.join(repo, *parts[i:])
        if os.path.isfile(cand):
            return os.path.abspath(cand)
    return None


def _validate_citations(out: str, repo: str) -> tuple[str, int, list[str]]:
    """Drop hallucinated citations and correct wrong-root paths.

    Returns (filtered_output, n_real_citations, citation_lines). A line that cites a path is
    kept only if the file exists (path rewritten to its real location); structural lines
    (<final_answer> tags) and pure prose are preserved in filtered_output. citation_lines is
    just the validated path lines (for aggregating across parallel runs). n_real == 0 means
    the model located nothing real — retry or fall back.
    """
    kept, citation_lines = [], []
    for ln in out.splitlines():
        m = _CITED_PATH_RE.search(ln)
        if not m:
            kept.append(ln)  # prose / structural line, no path claimed
            continue
        resolved = _resolve_cited_path(m.group(1), repo)
        if resolved:
            fixed = ln.replace(m.group(1), resolved, 1)
            kept.append(fixed)
            citation_lines.append(fixed)
        # else: hallucinated path — drop the line entirely
    return "\n".join(kept), len(citation_lines), citation_lines


def _citation_key(line: str) -> str:
    """Dedup key for a validated citation line: its path plus optional :line(-range)."""
    m = _CITED_PATH_RE.search(line)
    return (m.group(1) + (m.group(2) or "")) if m else line.strip()


# Directory names skipped while scanning — VCS/build/dependency dirs, not query words.
# (Skipping these is a generic perf guard, independent of what the user searched for.)
_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "build", "dist", "out", "target",
              "__pycache__", ".gradle", ".idea", ".venv", "venv", ".tox", "vendor"}


def _scan_files(query: str, repo: str, max_files: int = 20000):
    """Pure-stdlib scan (no external tool assumed). Reads each file once. Returns
    (terms, doc_freq, per_file) where per_file is a list of (relpath, present_terms,
    sample_lines) for every file containing at least one query term, and doc_freq maps
    each term to the number of files it appears in (for rarity weighting)."""
    # Stems (first 4 chars of words >=4 long) so morphological variants match the code:
    # environments->envi, staging->stag, production->prod, configured->conf. The length>=4
    # cutoff drops trivial words (the, and, are, dev) generically — no hardcoded stopword list.
    seen, terms = set(), []
    for t in re.findall(r"[A-Za-z]{4,}", query):
        stem = t.lower()[:4]
        if stem not in seen:
            seen.add(stem)
            terms.append(stem)
    if not terms:
        return [], {}, []

    doc_freq = {t: 0 for t in terms}
    per_file = []
    scanned = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in files:
            if scanned >= max_files:
                return terms, doc_freq, per_file
            path = os.path.join(root, fname)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read(1_000_000)  # ponytail: 1MB/file cap; raise if needed
            except OSError:
                continue
            if "\x00" in content[:1024]:
                continue  # binary
            scanned += 1
            low_content = content.lower()
            present = {t for t in terms if t in low_content}
            if not present:
                continue
            for t in present:
                doc_freq[t] += 1
            samples = []
            for n, line in enumerate(content.splitlines(), 1):
                ll = line.lower()
                if any(t in ll for t in present):
                    samples.append((n, line.strip()[:120]))
                    if len(samples) >= 6:
                        break
            per_file.append((os.path.relpath(path, repo), present, samples))
    return terms, doc_freq, per_file


def _rank_relevant_files(query: str, repo: str):
    """Rank files by how many DISTINCT query terms they contain (co-occurrence), tie-broken
    by term rarity — the file most "about" the query wins. Generic: common words spread
    across many files contribute little rarity weight, so no hardcoded stopwords are needed.
    Returns the sorted per_file list [(relpath, present_terms, sample_lines), ...]."""
    _, doc_freq, per_file = _scan_files(query, repo)
    if not per_file:
        return []

    def score(item):
        _, present, _ = item
        # Rarity-primary: a file rich in RARE query stems beats one matching only common
        # words. Coverage breaks ties. Common stems have high doc_freq -> ~0 contribution.
        rarity = sum(1.0 / doc_freq[t] for t in present)
        return (rarity, len(present))

    return sorted(per_file, key=score, reverse=True)


def _grep_fallback(ranked, top_files: int = 10) -> str | None:
    """Last resort when FastContext can't produce trustworthy citations: surface the top
    query-relevant files from the content scan (several, since the right one may not be #1).
    Lower-confidence hint; the agent verifies."""
    out = []
    for rel, present, samples in ranked[:top_files]:
        out.append(f"{rel}  (matches: {', '.join(sorted(present))})")
        for n, txt in samples[:2]:
            out.append(f"  {rel}:{n}  {txt}")
    if not out:
        return None
    return (
        "FastContext found no verifiable citations. Text-search fallback — files ranked by "
        "how many of your query terms they contain (lower confidence, verify; note the code "
        "may use abbreviations your query did not):\n" + "\n".join(out)
    )


# Cached resolved path to the Microsoft explorer CLI (None = not yet probed / not found).
_EXPLORER_BIN: str | None = None


def _is_explorer_cli(path: str) -> bool:
    """Verify `path` is the Microsoft `fastcontext` EXPLORER CLI, not our own
    `fastcontext-mcp` server or some unrelated binary that happens to share the name.

    The explorer's help advertises `--citation` and `--traj`; our MCP server has neither
    (it has --base-url/--model). A user may have both installed, so we check rather than
    trust the name.
    """
    try:
        r = subprocess.run([path, "--help"], capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001
        return False
    help_text = (r.stdout or "") + (r.stderr or "")
    return "--citation" in help_text and "--traj" in help_text


def _find_fastcontext() -> str | None:
    """Return the path to the Microsoft `fastcontext` explorer CLI, or None.

    Distinct from our `fastcontext-mcp` server. GUI apps on Mac/Linux often launch with a
    stripped PATH that omits ~/.local/bin, so we also probe the uv tool install locations.
    Each candidate is verified to actually be the explorer (see _is_explorer_cli). Result
    is cached for the process.
    """
    global _EXPLORER_BIN
    if _EXPLORER_BIN is not None:
        return _EXPLORER_BIN

    system = platform.system()
    if system == "Windows":
        base = os.environ.get("USERPROFILE", "")
        fallbacks = [
            os.path.join(base, r".local\bin\fastcontext.exe"),
            os.path.join(base, r".uv\bin\fastcontext.exe"),
        ]
    else:  # Darwin (macOS) or Linux
        fallbacks = [
            os.path.expanduser("~/.local/bin/fastcontext"),
            os.path.expanduser("~/.uv/bin/fastcontext"),
        ]

    candidates = []
    which = shutil.which("fastcontext")
    if which:
        candidates.append(which)
    candidates += [p for p in fallbacks if os.path.isfile(p) and os.access(p, os.X_OK)]

    for path in candidates:
        if _is_explorer_cli(path):
            _EXPLORER_BIN = path
            return path
    return None


def _not_found_hint() -> str:
    system = platform.system()
    install = "`uv tool install git+https://github.com/microsoft/fastcontext`"
    if system == "Windows":
        return (
            f"ERROR: `fastcontext` CLI not found. Run {install} in PowerShell "
            r"and ensure %USERPROFILE%\.local\bin is in your PATH. See docs/SETUP.md."
        )
    if system == "Darwin":
        return (
            f"ERROR: `fastcontext` CLI not found. Run {install} in a terminal. "
            "Mac GUI apps often strip PATH — restart the editor after installing, "
            "or add `~/.local/bin` to PATH in your shell rc. See docs/SETUP.md."
        )
    return (
        f"ERROR: `fastcontext` CLI not found. Run {install} "
        "and ensure `~/.local/bin` is in PATH. See docs/SETUP.md."
    )


def run_fastcontext(args: dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "ERROR: `query` is required and must be a non-empty exploration request."

    fc_bin = _find_fastcontext()
    if fc_bin is None:
        return _not_found_hint()

    if not CONFIG["model"]:
        return (
            "ERROR: no model configured. Pass --model <id> in this MCP server's `args` "
            "(the loaded model's identifier in LM Studio, or your endpoint's model name). "
            "See docs/SETUP.md."
        )

    repo = os.path.abspath(os.path.expanduser(args.get("repo_path") or CONFIG["repo"] or "."))
    if not os.path.isdir(repo):
        return f"ERROR: repo_path does not exist or is not a directory: {repo}"

    base_turns = int(args.get("max_turns") or CONFIG["default_max_turns"])
    citation_only = args.get("citation_only", True)
    user_set_turns = args.get("max_turns") is not None

    # Connection comes from MCP args -> injected into the subprocess env for the CLI.
    child_env = os.environ.copy()
    child_env["BASE_URL"] = CONFIG["base_url"]
    child_env["MODEL"] = CONFIG["model"]
    child_env["API_KEY"] = CONFIG["api_key"]

    log(f"exploring repo={repo}")

    def _run(turns: int):
        # Without --traj the CLI writes per-turn jsonl into .fastcontext/ inside the repo, so
        # we pass it but point at the OS null device — nothing is written, all in memory. The
        # CLI does makedirs(dirname(traj)): '/dev/null' has dirname '/dev' (exists); on Windows
        # the reserved name "NUL" is the null device in ANY directory, so we anchor it to repo.
        traj = os.path.join(repo, "NUL") if platform.system() == "Windows" else os.devnull
        cmd = [fc_bin, "-q", query, "--max-turns", str(turns), "--traj", traj]
        if citation_only:
            cmd.append("--citation")
        return subprocess.run(cmd, cwd=repo, env=child_env, capture_output=True,
                              text=True, timeout=CONFIG["timeout"])

    # FastContext samples non-deterministically (temperature 1.0) and can hallucinate paths,
    # so a single run is unreliable. We run several explorers IN PARALLEL at a spread of turn
    # budgets — each takes a different path — then validate every result (dropping hallucinated
    # paths, correcting wrong roots) and UNION the citations that actually exist on disk. This
    # leans on FastContext itself for recall; grep is only a last resort if all runs come up dry.
    # A pinned max_turns means one exact-budget run (the caller wants determinism/control).
    if user_set_turns:
        turn_budgets = [base_turns]
    else:
        hi = CONFIG["max_escalate_turns"]
        n = max(1, CONFIG["parallel_attempts"])
        lo = base_turns
        turn_budgets = [lo + round((hi - lo) * i / max(1, n - 1)) for i in range(n)]

    procs, timed_out, launch_err = [], 0, None
    with cf.ThreadPoolExecutor(max_workers=len(turn_budgets)) as ex:
        futs = [ex.submit(_run, t) for t in turn_budgets]
        for fut in cf.as_completed(futs):
            try:
                procs.append(fut.result())
            except subprocess.TimeoutExpired:
                timed_out += 1
            except Exception as exc:  # noqa: BLE001
                launch_err = exc

    # Aggregate validated citations across all runs. Independent stochastic explorers that
    # AGREE on a citation corroborate each other (consensus), so we count votes per path:line
    # and rank by agreement — the most-cited locations surface first, hallucinations (cited by
    # a single wandering run) sink to the bottom.
    votes, order, prose_best, nonzero_rc = {}, [], "", None
    n_ok = 0
    for proc in procs:
        candidate = (proc.stdout or "").strip()
        if proc.returncode != 0:
            nonzero_rc = (proc.returncode, (proc.stderr or candidate or "").strip())
            continue
        n_ok += 1
        validated, _, lines = _validate_citations(candidate, repo)
        run_keys = set()
        for ln in lines:
            key = _citation_key(ln)
            if key not in votes:
                votes[key] = [0, ln]
                order.append(key)
            if key not in run_keys:  # one vote per run
                votes[key][0] += 1
                run_keys.add(key)
            if len(ln) > len(votes[key][1]):  # keep the most descriptive phrasing
                votes[key][1] = ln
        if not citation_only and len(validated) > len(prose_best):
            prose_best = validated

    # Trust filter. A citation corroborated by >=2 independent runs is trusted on consensus.
    # A lone citation is trusted only if its file is among the query-relevant files (content
    # scan) — this drops "real path, fabricated description" hallucinations where the model
    # cites a real file whose content has nothing to do with the query. The scan is computed
    # lazily (only when there's a lone citation to vet, or nothing to fall back on).
    has_lone = any(votes[k][0] < 2 for k in order)
    ranked_files = _rank_relevant_files(query, repo) if (has_lone or not votes) else []
    relevant = {rel for rel, _, _ in ranked_files[:12]}

    trusted = []
    for k in order:
        if votes[k][0] >= 2:
            trusted.append(k)
            continue
        mm = _CITED_PATH_RE.search(votes[k][1])
        rel = os.path.relpath(mm.group(1), repo) if mm else ""
        if rel in relevant:
            trusted.append(k)
    dropped = len(order) - len(trusted)

    if citation_only and trusted:
        trusted.sort(key=lambda k: -votes[k][0])
        union_lines = [votes[k][1] + (f"  [{votes[k][0]}/{n_ok} runs agree]" if n_ok > 1 else "")
                       for k in trusted]
        if dropped:
            log(f"dropped {dropped} unverifiable lone citation(s)")
        log(f"unioned {len(union_lines)} verified citations across {n_ok} runs "
            f"(top consensus {votes[trusted[0]][0]}/{n_ok})")
        out = "<final_answer>\n" + "\n".join(union_lines) + "\n</final_answer>"
    elif not citation_only and prose_best.strip():
        out = prose_best
    else:
        # Nothing trustworthy. Surface a concrete error if every run failed/timed out.
        if not procs and timed_out:
            return (
                f"ERROR: all {timed_out} FastContext explorers timed out after "
                f"{CONFIG['timeout']}s. Narrow the query or raise --timeout."
            )
        if launch_err and not procs:
            return f"ERROR: failed to launch fastcontext: {launch_err!r}"
        if nonzero_rc and not votes:
            code, msg = nonzero_rc
            return f"ERROR: fastcontext exited with code {code}.\n{msg[: CONFIG['max_chars']]}"
        fallback = _grep_fallback(ranked_files)
        if fallback:
            log(f"no trustworthy citations (dropped {dropped}) — served co-occurrence fallback")
            return fallback
        return (
            "FastContext explored but produced no citations that exist on disk (it can "
            "hallucinate paths). Next steps: (1) call again — it samples non-deterministically; "
            "(2) sharpen the query to name the exact symbol; (3) pass repo_path for the right "
            "monorepo subfolder."
        )

    if len(out) > CONFIG["max_chars"]:
        out = out[: CONFIG["max_chars"]] + f"\n... [truncated at {CONFIG['max_chars']} chars]"
    return out


def make_result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def make_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle(msg: dict):
    """Return a response dict for requests, or None for notifications."""
    method = msg.get("method")
    req_id = msg.get("id")
    is_request = req_id is not None

    if method == "initialize":
        params = msg.get("params") or {}
        protocol = params.get("protocolVersion") or DEFAULT_PROTOCOL
        return make_result(
            req_id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method == "notifications/initialized":
        return None

    if method == "ping":
        return make_result(req_id, {}) if is_request else None

    if method == "tools/list":
        return make_result(req_id, {"tools": [TOOL]})

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        if name != TOOL["name"]:
            return make_error(req_id, -32602, f"Unknown tool: {name}")
        text = run_fastcontext(params.get("arguments") or {})
        return make_result(
            req_id,
            {"content": [{"type": "text", "text": text}], "isError": text.startswith("ERROR:")},
        )

    if is_request:
        return make_error(req_id, -32601, f"Method not found: {method}")
    return None


def parse_args(argv=None) -> None:
    p = argparse.ArgumentParser(description="FastContext MCP server (connection via args, no env vars)")
    p.add_argument("--base-url", default=CONFIG["base_url"],
                   help="OpenAI-compatible base URL (default: LM Studio %(default)s)")
    p.add_argument("--model", default=CONFIG["model"],
                   help="Model identifier served by your endpoint (required to run a query)")
    p.add_argument("--api-key", default=CONFIG["api_key"],
                   help="API key; overridden by FASTCONTEXT_API_KEY env var (keeps key out of args/ps); local servers ignore it (default: %(default)s)")
    p.add_argument("--repo", default=CONFIG["repo"],
                   help="Default repository root to explore (default: the server's cwd)")
    p.add_argument("--default-max-turns", type=int, default=CONFIG["default_max_turns"],
                   help="Default exploration turns when a call omits max_turns (default: %(default)s)")
    p.add_argument("--max-escalate-turns", type=int, default=CONFIG["max_escalate_turns"],
                   help="Deepest turn budget among the parallel explorers (default: %(default)s)")
    p.add_argument("--parallel-attempts", type=int, default=CONFIG["parallel_attempts"],
                   help="Concurrent explorations whose verified citations are unioned (default: %(default)s)")
    p.add_argument("--timeout", type=int, default=CONFIG["timeout"],
                   help="Per-exploration timeout in seconds (default: %(default)s)")
    p.add_argument("--max-chars", type=int, default=CONFIG["max_chars"],
                   help="Cap on returned characters (default: %(default)s)")
    a = p.parse_args(argv)
    # ponytail: env var wins over arg so the key never appears in ps aux or client config args
    api_key = os.environ.get("FASTCONTEXT_API_KEY") or a.api_key
    CONFIG.update(
        base_url=a.base_url,
        model=a.model,
        api_key=api_key,
        repo=a.repo,
        default_max_turns=a.default_max_turns,
        max_escalate_turns=a.max_escalate_turns,
        parallel_attempts=a.parallel_attempts,
        timeout=a.timeout,
        max_chars=a.max_chars,
    )


def _check_for_update() -> None:
    """Fetch pyproject.toml from GitHub main and log if a newer version is available.

    Runs once at startup. Silent on any network or parse error so it never blocks the server.
    """
    try:
        import urllib.request
        url = "https://raw.githubusercontent.com/LIVELUCKY/fastcontext-integrations/main/pyproject.toml"
        with urllib.request.urlopen(url, timeout=3) as r:
            for line in r.read().decode().splitlines():
                if line.startswith("version"):
                    latest = line.split('"')[1]
                    if latest != SERVER_VERSION:
                        log(
                            f"update available: v{latest} (you have v{SERVER_VERSION}) — "
                            "restart with: uvx --refresh --from "
                            "git+https://github.com/LIVELUCKY/fastcontext-integrations fastcontext-mcp"
                        )
                    break
    except Exception:  # noqa: BLE001
        pass  # offline, rate-limited, or GitHub down — not worth surfacing


def _detect_variant(model_id: str) -> dict:
    """Parse a FastContext model ID into {size, training, backend, is_fastcontext}.

    Handles both canonical IDs (FastContext-1.0-4B-RL) and runtime-renamed variants
    (fastcontext-1.0-4b-sft-dynamic-mlx).  Case-insensitive throughout.
    """
    m = (model_id or "").lower()
    is_fc = "fastcontext" in m
    size = "30b" if "30b" in m else "4b" if "4b" in m else None
    training = "rl" if "-rl" in m else "sft" if "sft" in m else None
    backend = "mlx" if "mlx" in m else "gguf" if "gguf" in m else None
    return {"size": size, "training": training, "backend": backend, "is_fastcontext": is_fc}


# Recommended max_turns per model size (user override via --default-max-turns always wins).
_TURNS_BY_SIZE = {"4b": 8, "30b": 10}


def _apply_variant_defaults(variant: dict, user_set_turns: bool) -> None:
    """Adjust CONFIG defaults based on detected variant and log a startup summary."""
    if not variant["is_fastcontext"] and CONFIG["model"]:
        log(
            f"warning: model '{CONFIG['model']}' doesn't look like a FastContext model. "
            "Expected IDs like FastContext-1.0-4B-SFT, FastContext-1.0-4B-RL, "
            "FastContext-1.0-30B-SFT. Exploration may still work but results could be poor."
        )

    if not user_set_turns and variant["size"]:
        CONFIG["default_max_turns"] = _TURNS_BY_SIZE.get(variant["size"], CONFIG["default_max_turns"])

    parts = [
        f"size={variant['size'] or '?'}",
        f"training={variant['training'] or '?'}",
        *([ f"backend={variant['backend']}"] if variant["backend"] else []),
        f"max_turns={CONFIG['default_max_turns']}",
    ]
    log("variant: " + "  ".join(parts))


def main() -> None:
    parse_args()
    variant = _detect_variant(CONFIG["model"] or "")
    # ponytail: track whether the user explicitly set turns so we don't clobber their choice
    user_set_turns = "--default-max-turns" in sys.argv
    _apply_variant_defaults(variant, user_set_turns)
    log(f"ready — base_url={CONFIG['base_url']} model={CONFIG['model'] or '(unset: pass --model)'}")
    _check_for_update()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log(f"skipping non-JSON line: {line[:200]}")
            continue
        try:
            response = handle(msg)
        except Exception as exc:  # noqa: BLE001
            response = make_error(msg.get("id"), -32603, f"Internal error: {exc!r}")
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
