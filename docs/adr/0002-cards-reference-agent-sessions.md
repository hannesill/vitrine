---
status: proposed
---

# Cards reference agent sessions; hosts supply the details

A card should show which agent session created it and which sessions edited it, including things
only the host knows: the session's title, the model behind each turn, and a way back into the
conversation. A card stores only stable identifiers. The host pushes the changeable details to
Vitrine as separate records, and the card page joins the two when someone views it. Titles get
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

A host that runs while cards are being made pushes one record per agent session to the Vitrine
daemon, using the same authentication as other write requests, and pushes again whenever
something changes:

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

Vitrine keeps these records in the data directory, next to the studies. When a card page renders,
Vitrine matches each agent context to its session record and to the turn that contains
`recorded_at`. `open_url` is whatever link the host uses to open that session.

For hosts that cannot push, Vitrine builds the record itself from files on disk: for
`claude-code`, from the session transcript under `~/.claude/projects/`. Any other context without
a record shows only its stored identifiers.

## Considered options

- **Vitrine asks the host when the card is viewed.** A request to the host's server would need the
  host's address and credentials. A message to the embedding page breaks when the page is not
  embedded, and Lincei shows the board in a `<webview>` that a guest page cannot simply post to.
  Pushing reverses the direction: the host already knows where the daemon is, because it started
  it.
- **Store title and model on the card.** Rejected because both change after the card exists.

## Consequences

- An exported study copies the matched session details into the file, because no host is present
  when someone opens it later.
- Without a running host, the card page shows whatever the host last pushed.
- "Session" also names two unrelated things in Vitrine's code: the server instance
  (`_session_id`) and the legacy store directory (`ArtifactStore(session_dir, session_id)`). Both
  should get other names, so that "session" in Vitrine only ever means an agent session.
