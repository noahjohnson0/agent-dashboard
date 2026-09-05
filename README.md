# agent-dashboard

A local cockpit for a GitHub repo: see your issues, dispatch Claude Code or Codex
agents at them, and chat with several at once in a dockable side panel.

Two files, no dependencies, no build step. It bolts onto any repo — run it inside
a checkout and it works out what repo you are in.

```
python3 server.py     # then open http://localhost:8787
```

## What it does

**Issues.** Every open/closed issue with its author, labels, and a live status:
`todo`, `claimed by <user>`, `agent dispatched`, `agent working`, `in review (PR #n)`.
The list caches locally, refreshes every 60s while visible, and cascades in.

**Dispatch an agent.** Choose Claude or Codex and Plan or Build above the list,
then press the robot button on an issue. The dashboard hands the issue to that
CLI and opens a chat tab for it. Several agents can run
at once, one tab each, with a vertical stage checklist at the top of the chat
showing what is done, what is running, and what is still ahead.

Two modes:

- **Plan** (default) — read-only. The agent investigates and reports back; it
  cannot change files.
- **Build** — the agent gets its own `git worktree` and branch off `origin/main`,
  does the work, and opens a draft PR. It runs with its CLI's permission prompts
  bypassed inside that worktree, so only turn it on for repos where you want that.

**Dispatch state lives on GitHub.** Dispatching applies an `agent-dispatched`
label to the issue (created on first use), so the state survives restarts and is
visible to everyone on the repo. Dismissing an agent removes it.

**New issue.** Describe the problem in the panel; an agent reads the repo, writes
it up properly, and files it with `gh`.

**Attachments.** Drop files anywhere on the panel, paste a screenshot, or use the
attach button. Images go to the agent as images (`-i` for Codex, `Read` for
Claude); anything else — a log, a diff, a CSV — is handed over as a path it can
read. Up to 8 MB per file.

**Roadmap.** Point it at a markdown tracker and it renders a progress meter,
a timeline, and expandable sections whose rows link to the issues they mention
(and can dispatch an agent on them).

**Stats.** Live machine load, memory, watched processes and optional server ping,
sampled every 15s and kept for a day, plus weekly issue throughput from GitHub.

**Project buttons.** Configure a launch command, test suites and a process-name
pattern, and you get Launch / Run tests / Kill buttons — for the main checkout,
or for a specific agent's branch. Output streams into the panel's Console tab.

## Requirements

- Python 3.9+ (standard library only)
- [`gh`](https://cli.github.com) — authenticated (`gh auth login`)
- `git`
- At least one agent CLI: [`claude`](https://claude.com/claude-code) or
  [`codex`](https://developers.openai.com/codex/cli)

Works on macOS, Linux and Windows.

## Configuration

Everything is auto-detected when you start the server inside a checkout. To
override, create `config.json` in the state directory:

| Platform | State directory |
|---|---|
| macOS / Linux | `~/.local/state/agent-dashboard/` |
| Windows | `%USERPROFILE%\.local\state\agent-dashboard\` (or set `DASHBOARD_STATE`) |

```json
{
  "repo": "owner/name",
  "repoDir": "~/code/my-project",
  "worktrees": "~/code/my-project-worktrees",
  "tracker": "ROADMAP.md",
  "gameHost": "server.example.com",
  "launch": ["npm", "run", "dev"],
  "killPattern": "node",
  "suites": [
    ["unit", "npm", "test"],
    ["e2e", "npx", "playwright", "test"]
  ]
}
```

`launch` takes either a bare command or, when something has to happen first,
a list of named steps run in order — they stop at the first failure:

```json
"launch": [
  ["build", "npm", "run", "build"],
  ["dev", "npm", "run", "dev"]
]
```

Every key is optional. `launch`, `suites` and `killPattern` are empty by default
and their buttons stay hidden until you set them. `tracker` defaults to the first
of `ROADMAP.md`, `ALPHA_RELEASE_TRACKER.md`, `docs/ROADMAP.md`, `TRACKER.md` that
exists. Env vars `DASHBOARD_REPO`, `DASHBOARD_REPO_DIR`, `DASHBOARD_WORKTREES`,
`DASHBOARD_TRACKER`, `DASHBOARD_GAME_HOST`, `DASHBOARD_STATE` and `PORT` win over
the file.

The server binds `127.0.0.1` only. It runs local commands and agent CLIs as you,
so do not expose it to a network.

## Setup, for your agent

Paste this to Claude Code, Codex, or whatever you use:

> Set up the agent-dashboard from https://github.com/noahjohnson0/agent-dashboard
> for this repo.
>
> 1. Clone it somewhere outside this repo (e.g. `~/tools/agent-dashboard`) and
>    confirm `python3 --version` is 3.9+, `git`, and `gh` (run `gh auth status`;
>    if it is not authenticated, tell me to run `gh auth login` myself — do not
>    run interactive logins).
> 2. Confirm at least one agent CLI is on PATH: `claude --version` or
>    `codex --version`. Tell me which are available.
> 3. Work out this project's commands: how the app is started for local
>    development, how its test suites are run, and the process name to match when
>    killing strays. Read the repo's README / AGENTS.md / CLAUDE.md / package
>    manifest rather than guessing, and tell me what you found.
> 4. Write `config.json` in the state directory for my platform
>    (`~/.local/state/agent-dashboard/` on macOS and Linux,
>    `%USERPROFILE%\.local\state\agent-dashboard\` on Windows) with `repo`,
>    `repoDir` (absolute path to this checkout), `launch`, `suites` and
>    `killPattern` filled in from step 3. Leave out anything you are unsure of.
>    Add `tracker` only if this repo has a markdown roadmap or release tracker.
> 5. Start it with `python3 server.py` from the dashboard directory, check
>    `http://localhost:8787/api/me` returns this repo, and give me the URL.
>
> Do not commit anything to this repo, and do not run the launch or test commands
> unless I ask.

## Notes

- Agents live in the server's memory: restarting it drops running agents, though
  their worktrees, branches, PRs and GitHub labels remain.
- Build mode runs an agent CLI with permission prompts bypassed inside its own
  worktree. That is the point of it, and it is worth understanding before you use it.
- A build agent is told to follow the repo's own conventions file (`AGENTS.md`,
  `CLAUDE.md`) — the better that file is, the better the PRs are.

## License

MIT
