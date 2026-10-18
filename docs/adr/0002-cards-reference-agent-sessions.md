---
status: proposed
---

# Cards reference agent sessions; hosts supply the details

A card should show which agent session created it and which sessions edited it, including things
only the host knows: the session's title, the model behind each turn, and a way back into the
conversation. A card stores only stable identifiers. The host records the changeable details
separately, and the card page joins the two when someone views it. Titles get
renamed and a session can switch models between turns, so copying those values onto the card
would leave stale or wrong data.

An **agent session** is one conversation with an agent inside a **host**, the program that runs
the agent: a Lincei thread, a Claude Code session, or a Codex session.

## What a card stores

Card creation and every `replace=` each record one agent context:

```json
{
  "host": "lincei",
  "session_id": "…",
  "turn_id": null,
  "recorded_at": "2026-10-16T14:02:11Z"
}
```

- `host`: `lincei`, `claude-code`, `codex`, or `unknown`.
- `session_id`: the host's identifier for the agent session, or null.
- `turn_id`: the host's identifier for the turn, when the host can provide it. Otherwise the turn
  is found from `session_id` and `recorded_at`.
- `recorded_at`: when Vitrine recorded the context, in UTC.

Together, the creation context and the context of each revision answer "which sessions created
and edited this card".

At `show()` Vitrine fills the context from the first source that applies:

1. `VITRINE_AGENT_HOST`, `VITRINE_AGENT_SESSION_ID` and `VITRINE_AGENT_TURN_ID`, which any host
   can set. Lincei sets them when it launches a provider for a thread.
2. `CLAUDE_CODE_SESSION_ID`, giving host `claude-code`.
3. Codex: to be decided after checking what the Codex CLI passes to child processes.
4. Otherwise host `unknown` and no session.

The study-level `session_id` in `meta.json` stays for one job: the study list's resume button,
which copies `claude --resume <id>` for the study's latest Claude Code session. It used to be read
from `CLAUDE_SESSION_ID`, which Claude Code does not set, so the button never appeared. It now
reads `CLAUDE_CODE_SESSION_ID`.

## What hosts supply

A host records each agent session with `vitrine record-session`, which reads the record as JSON
on stdin, or with `vitrine.record_agent_session()`, and records again whenever something changes,
such as a new title or a new turn:

```json
{
  "host": "lincei",
  "session_id": "…",
  "title": "Sepsis cohort, lactate exclusion",
  "open_url": "…",
  "turns": [
    { "turn_id": "…", "started_at": "…", "provider": "claude", "model": "…" }
  ]
}
```

Both write `agent_sessions.json` in the data directory, next to the studies, so no server needs
to be running. Fields left out keep their stored values, and turns merge by `turn_id`. When a
card page renders, Vitrine matches each agent context to its session record and to its turn: the
one with the card's `turn_id`, else the last that started before `recorded_at`. `open_url` is
whatever link the host uses to open that session; `javascript:`, `data:` and `file:` links are
refused.

For Claude Code, which runs no host process to record anything, Vitrine reads the session's
transcript (`~/.claude/projects/*/<session_id>.jsonl`; `CLAUDE_CONFIG_DIR` moves `~/.claude`): its
latest custom title, and a turn for each model the main agent switched to. Any other context
without a record shows only its stored identifiers.

## Considered options

- **Vitrine asks the host when the card is viewed.** A request to the host's server would need the
  host's address and credentials. A message to the embedding page breaks when the page is not
  embedded, and Lincei shows the board in a `<webview>` that a guest page cannot simply post to.
  Recording reverses the direction: the host already knows the data directory, because it sets
  `VITRINE_DATA_DIR` for its agents.
- **An HTTP endpoint on the daemon for hosts to post to.** It would need the server to be running
  and the host to hold its access token. The CLI is already how hosts drive Vitrine.
- **Store title and model on the card.** Rejected because both change after the card exists.

## Consequences

- Exports do not include card pages yet. When they do, they must copy the matched session details
  into the file, because no host is present when someone opens it later.
- Without a running host, the card page shows whatever the host last recorded.
- "Session" also names two unrelated things in Vitrine's code: the server instance
  (`_session_id`) and the legacy store directory (`ArtifactStore(session_dir, session_id)`). Both
  should get other names, so that "session" in Vitrine only ever means an agent session.
