# Memory System

Long-term memory is split in two layers (restructured 2026-09-17):

1. `MAINMEMORY.md` — a THIN INDEX: who the user is, a few standing facts, open questions,
   and one-line pointers to topic notes. Target 60–120 lines. Content never lives here.
2. Topic notes in the Obsidian vault `/root/obsidian-vault`:
   - `Personal/` — the user himself (profile, relocation, hardware, side projects, `Health/`)
   - `Clowder/` — employer product and ASI context
   - `TheTop/` — consulting client (Jira DEV, AI Assist, Agent X, TTE landing)
   - `AI/Albert/` — the assistant itself (infrastructure, crons, bridge, wall, sub-agents, models, skills)
   - `AI/Rules/` — behavior rules and preferences, one file per area (`Index.md` lists them)
   Every folder has an `Index.md` hub. The vault is a private git repo, auto-synced every 5 min.

## Silence Is Mandatory

Never tell the user you are reading or writing memory. Memory operations are invisible.

## Read First

At the start of a session read `MAINMEMORY.md` (small). Then open only the topic notes the
task needs. Before Jira work read the matching `AI/Rules/Jira *.md`; before specs read
`AI/Rules/Product Specs.md`; before anything about tone read `AI/Rules/Communication and Tone.md`.
Do not read the whole vault "just in case" — the point of the split is to keep context small.

## When to Write

- Durable personal facts or preferences
- Decisions that should affect future behavior
- User explicitly asks to remember
- Repeating workflow patterns, traps and their fixes
- Cron/webhook setup signals that imply interests

## Where to Write (strict)

1. Find the topic note the fact belongs to and append a dated bullet `(YYYY-MM-DD)`.
   Merge with an existing bullet if it is about the same thing; delete what it supersedes.
2. No fitting note → create one in the right folder, add a line to that folder's `Index.md`
   and ONE pointer line to `MAINMEMORY.md` (`- Topic → path.md — one sentence`).
3. Behavior rules and preferences → `AI/Rules/<area>.md`, never into the index.
4. Open questions to the user → the "Open questions" section of `MAINMEMORY.md`; remove when answered.
5. Language: English for all memory notes (fewer tokens). Exception: `Personal/Health/` stays Russian.
   Keep identifiers, paths, IDs, keys, numbers verbatim.
6. Anything longer than one sentence in `MAINMEMORY.md` is a bug — move it to a note.

## When Not to Write

- One-off throwaway requests
- Temporary debugging noise
- Facts already recorded

## Shared Knowledge (SHAREDMEMORY.md)

Facts relevant to ALL agents (server facts, user preferences, infrastructure, conventions):

```bash
python3 tools/agent_tools/edit_shared_knowledge.py --append "New shared fact"
```

The Supervisor syncs it into every agent's `MAINMEMORY.md` between the
`--- SHARED KNOWLEDGE START/END ---` markers. Never edit or remove those markers.

## Cleanup Rules

- If the user says data is wrong or should be forgotten, fix the topic note immediately.
- No "deleted" markers; keep files clean.
- If `MAINMEMORY.md` grows past ~120 lines, someone is writing content into it — fix the
  cause and move the content out; do not "compress" the index.
- Snapshot of the pre-split memory: `archive/2026-09-17/`.
