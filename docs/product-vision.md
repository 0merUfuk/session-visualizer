# Rifja product vision — agent execution layer

Status: **Ratified product direction** (owner directive, 2026-09-12). This
document supersedes the usage-model framing of earlier documents; the
engineering boundaries they define (threat model, JSON contract, release
discipline) remain in force.

## The product in two sentences

**Install the tool, tell your AI agent to use it, and let the agent do the
work. Watch and manage what happens from the dashboard.**

The primary user of Rifja's tool surface is the **AI agent** — Claude Code,
Codex, Cursor, or any MCP-capable coding agent the operator already runs.
The human operator talks to their agent in natural language and observes,
configures and controls Rifja through the web dashboard. Rifja never sells or
embeds an AI: the intelligence sits on the user's side; Rifja is the reliable,
well-designed tool layer that extends it.

## What Rifja is

1. **An execution layer for agents.** The CLI is not the human's primary
   interface — it is the machine surface agents drive (directly, or through
   the MCP server). Every command is deterministic, structured and
   machine-first: stable JSON contract (`schema_version`), contract-label
   errors with hints, stable exit codes.
2. **A continuity memory with receipts.** Agents forget, compact and lose
   history; producers delete transcripts. Rifja keeps imported evidence with
   provenance, authority separation and operator-accepted memory — offline,
   no cloud, no telemetry.
3. **An observability panel for the operator.** The dashboard shows what the
   agent did through Rifja (activity/execution history), the tool's state,
   coverage and configuration. It is a management, observability and
   configuration layer — not the product's main usage point and not a
   replacement for the CLI.

## Principles

- **Agent-first design.** New capabilities are designed as agent-usable first
  (MCP tool, structured I/O, described in agent idioms); human CLI and
  dashboard views follow. If an agent cannot do it, the flow is incomplete.
- **Natural language is the primary human interface.** "Set up Rifja and
  import my Codex sessions" spoken to the agent must be sufficient. The
  dashboard is the human's management surface; direct CLI and wizard screens
  exist for humans who want them, never as the required path.
- **The operator's authority is never delegated to the agent.** Agents may
  propose memory (`inferred`/`proposed`), never accept it. Destructive
  operations (forget, retention, backup, restore) are not exposed to agents.
  Registration remains explicit; nothing is scanned implicitly.
- **Determinism over cleverness.** No LLM, embeddings or probabilistic
  retrieval inside Rifja. Same input, same output — that is why an agent can
  trust it.
- **Stdlib-only runtime, offline by construction.** Zero runtime
  dependencies; no network egress; the dashboard is loopback and read-only.
  This is a product property and a moat, not a limitation to work around.
- **The dashboard must feel engineered, not generated.** One deliberate
  design system (typography, spacing, color, density), information-dense,
  developer-oriented, dark-first. No decorative gradients, card soup, glass
  effects or generic SaaS layout.

## Architecture consequences

1. **MCP server is the primary surface.** It carries read tools *and* the
   operational tools an agent needs (setup, registration, refresh, queries).
   Mutations preserve the explicit-consent model and are limited to
   non-destructive operations.
2. **Activity is a first-class record.** Every agent-driven operation is
   logged (append-only `activity` table: tool, summary, status, duration).
   The dashboard's home screen is this activity feed — the operator sees the
   agent's executions, failures included.
3. **Dashboard = management plane.** Information architecture: Activity,
   Overview, Sessions/Evidence, Search, Memory, Sources, Settings. Pages are
   bounded and fast (aggregates first, drill-down on demand, pagination,
   caching, compression); the design language is monospace-first,
   signal-oriented and dense.
4. **Setup has an agent path.** Non-interactive primitives plus agent
   onboarding (integration snippets, skills) make "agent, set this up" a
   complete flow. The interactive wizard remains one option, not the gate.
5. **Docs speak two audiences.** Agent-facing material (tool descriptions,
   skill files, machine-readable references) is as important as human docs.

## Non-goals (unchanged and reinforced)

No embedded or sold AI, no cloud service, no telemetry, no orchestrator or
agent runtime, no write-back to producers, no plugin runtime beyond reviewed
adapters, no SPA framework or frontend build step. Rifja extends the agent
the user already has; it does not compete with it.
