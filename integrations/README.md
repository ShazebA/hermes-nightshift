# Coding-agent integrations

Nightshift writes a brief per failure. These integrations let a coding agent go from "a brief exists" to "I'm fixing it" in one step:

- **Pull:** `nightshift next --json` lists unclaimed briefs that have not gone green again, newest first. `nightshift claim ID --agent NAME` records who took the brief (`handoff.claimed_by`/`claimed_at`), so two agents don't fix the same thing. If `kanban.enabled`, it also comments on the brief's card and unblocks it. `claim` exits 1 when the brief is already claimed or already resolved. When your fix goes green, the next poll resolves the brief and completes its card.
- **Push (optional):** with `aetherbus.enabled: true`, every brief is also published to AetherBus. `aetherbus-mcp` exposes the topic as an MCP resource, so the agent sees new briefs as they land. The event's `dataref` is a `file://` URI to the brief JSON, so the agent needs read access to `briefs_dir`.

The agent needs `nightshift` on its `PATH` and the same config Nightshift uses: `./nightshift.yaml`, `$NIGHTSHIFT_CONFIG`, or `~/.config/nightshift/nightshift.yaml`.

> **Kanban note:** `hermes kanban unblock` moves the card to `ready`. If the card has an assignee (from `kanban.assignee` or `kanban.default_assignee`), a running Hermes gateway may dispatch a worker for it while your coding agent is also fixing it. The claim comment says the work is happening outside Hermes, but leave Nightshift cards unassigned if you use `claim`.

## MCP server (all three agents)

Every agent below runs the same server:

```bash
aetherbus-mcp serve --topic dev.aetherbus.nightshift.brief.v1
```

It reads the broker URL from `AETHERBUS_URL`, or from `--bus nats://HOST:4222` or `--config aetherbus.yaml`. Install it with `pip install 'aetherbus[mcp]'`.

## Claude Code

Slash command: copy [`claude-code/commands/nightshift-fix.md`](claude-code/commands/nightshift-fix.md) into the repo's `.claude/commands/`, or into `~/.claude/commands/` to use it in every repo. Then run `/nightshift-fix` or `/nightshift-fix acme/webapp`. It runs `nightshift next`, claims the top brief, reads the Markdown brief, runs its repro, and fixes on a `nightshift/<id>` branch.

MCP: add this to `.mcp.json` at the repo root.

```json
{
  "mcpServers": {
    "aetherbus": {
      "command": "aetherbus-mcp",
      "args": ["serve", "--topic", "dev.aetherbus.nightshift.brief.v1"],
      "env": {"AETHERBUS_URL": "nats://localhost:4222"}
    }
  }
}
```

Or register it from the shell:

```bash
claude mcp add aetherbus -- aetherbus-mcp serve --topic dev.aetherbus.nightshift.brief.v1
```

## Codex

MCP: Codex takes MCP servers from `~/.codex/config.toml`. Check the Codex docs for the current key names.

```toml
[mcp_servers.aetherbus]
command = "aetherbus-mcp"
args = ["serve", "--topic", "dev.aetherbus.nightshift.brief.v1"]
env = { AETHERBUS_URL = "nats://localhost:4222" }
```

Workflow: the steps in `nightshift-fix.md` below the front matter are plain instructions. Save them as a Codex custom prompt, or paste them in. Use `--agent codex` when claiming.

## Cursor

MCP: Cursor uses the same `mcpServers` shape as Claude Code, in `.cursor/mcp.json` (per project) or `~/.cursor/mcp.json` (global). Copy the JSON block above.

Workflow: reuse the body of `nightshift-fix.md` as a Cursor command or rule, and claim with `--agent cursor`. See the Cursor docs for where commands live.
