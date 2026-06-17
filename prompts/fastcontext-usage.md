# FastContext usage prompt (drop-in)

Paste the block below into your agent's instruction file (`CLAUDE.md`, `AGENTS.md`,
`.github/copilot-instructions.md`, a Cursor rule, etc.). It tells the agent when and
how to delegate repo exploration to the `fastcontext_explore` MCP tool.

---

## Repository exploration with FastContext

You have access to **FastContext** via the `fastcontext_explore` MCP tool — a fast,
read-only subagent that explores this codebase autonomously and returns `file:line`
citations. Call it with a specific natural-language `query`.

**Use FastContext before:**
- Editing, reviewing, debugging, or explaining any code you are not already certain about
- Tracing logic across functions, files, or layers (request → handler → service → DB)
- Answering "where is X defined", "what calls Y", "what does Z depend on"
- Making a change whose impact you cannot assess from the files you have already read

**Skip FastContext when:**
- The task names the exact file and line range to change
- A previous call this session already returned the relevant locations
- You need to search within 2–3 files you have already read this turn

**After FastContext returns:**
- Trust the listing. Open only the named files at the named line ranges.
- Do **not** repeat broad searches (`grep -R`, `find . -name`) for the same information.
- Read narrowly: a 30–80 line window around the cited symbol is usually enough.
- If the result feels incomplete, re-ask with a sharper query — faster than scanning yourself.

**Writing good queries** — name the behavior, symbol, error, or subsystem:
- Good: `"Find where incoming webhook signatures are verified and where the secret is loaded"`
- Weak: `"how does the backend work"`
