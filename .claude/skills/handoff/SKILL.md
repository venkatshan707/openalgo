---
name: handoff
description: Write session handoff files to My_Docs/handoffs/ so the next session can pick up context. Always writes two files: latest_<session_name>.md (overwritten each time) and a timestamped archive. Invoke via /handoff.
argument-hint: "[optional session name override, otherwise derive from current topic]"
allowed-tools: Read, Write, Glob
---

# Session Handoff

Write two handoff files to `My_Docs/handoffs/` so any future Claude Code session can load them for full context on what was happening, what was decided, and why.

## Session name

Derive the session name from the primary topic of the conversation (e.g. `nifty-channel-strategy`, `broker-integration`, `options-flow`). If the user passed an argument, use that. Keep it lowercase-kebab-case.

## Files to write

**1. `My_Docs/handoffs/latest_<session_name>.md`** — always overwritten. Contains the current state.

**2. `My_Docs/handoffs/handoff_<session_name>_<YYYY-MM-DDTHHMM>.md`** — permanent archive. Never overwrite an existing timestamped file; always write a new one. Use IST time (UTC+5:30) for the timestamp.

## Content structure

Both files get identical content:

```markdown
# Handoff: <Session Name>

**Date:** YYYY-MM-DD HH:MM IST
**Project:** OpenAlgo — Nifty Channel Options Strategy (or whatever the project is)

## What we were working on

[1-3 sentences: the concrete task in progress at the end of the session]

## Decisions made

| Decision | Why |
|---|---|
| [decision] | [reason] |

List every significant architectural, algorithmic, or product choice made during the session. One row per decision. Include things that were *ruled out* and why — those are often the most valuable.

## Key findings

[Bullet list: quantitative results, compiler errors fixed, parameter values confirmed, behaviour discovered. Concrete numbers where available.]

## Current state

**Done:**
- [list what is complete and working]

**In progress / next steps:**
- [list what was being worked on when the session ended, in priority order]

## Files changed or created

| File | Status | Notes |
|---|---|---|
| [path] | created/modified/pending | [one-line note] |

## Context a new session needs to know

[Anything non-obvious: known bugs, environment gotchas, constraints, or instructions that are not in the code. This section is the most important — write it for someone who has never seen this project.]
```

## Instructions

1. Read the conversation context carefully. This is a synthesis task, not a dump — write for a trader-developer who has 30 seconds to get up to speed.
2. Derive the session name if not provided.
3. Get the current IST timestamp.
4. Write both files. Confirm the file paths at the end.
5. Do not include internal Claude reasoning, tool call details, or raw error messages — only trader-readable facts and decisions.
