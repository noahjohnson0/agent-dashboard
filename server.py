#!/usr/bin/env python3
"""agent-dashboard — a local cockpit for any GitHub repo.

Run it inside a checkout and it bolts on: it reads the repo from `gh`, lists
issues, and lets you dispatch Claude or Codex agents at them.

  * Issue list with author, labels, and live status (claimed / agent working / in review)
  * Roadmap view parsed from a markdown tracker (auto-detected, configurable)
  * Stats view: live machine + server samples, historical issue throughput
  * Per-issue agents you chat with in a dockable side panel, several at once
  * Dispatch state tracked on GitHub itself with the `agent-dispatched` label
  * Build-mode agents get their own git worktree + branch and open a draft PR
  * Configurable run/launch/kill buttons for whatever this project needs

macOS, Linux and Windows. Python 3.9+, no third-party packages.
"""
import calendar, http.server, json, os, platform, re, shlex, shutil, signal
import subprocess, sys, threading, time, uuid

WINDOWS = platform.system() == "Windows"

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.expanduser(os.environ.get(
    "DASHBOARD_STATE", "~/.local/state/agent-dashboard"))
STATS_FILE = os.path.join(STATE_DIR, "stats.jsonl")
CONFIG_FILE = os.path.join(STATE_DIR, "config.json")

# Everything project-specific lives here. Empty values are auto-detected from
# the checkout you start the server in; override any of them with env vars or
# with <state dir>/config.json.
DEFAULTS = {
    "repo": "",            # owner/name on GitHub; "" -> ask gh in repoDir
    "repoDir": "",         # primary checkout;      "" -> current directory
    "worktrees": "",       # where build agents get their trees; "" -> <repoDir>-worktrees
    "tracker": "auto",     # roadmap markdown, relative to repoDir; "auto" -> first found
    "gameHost": "",        # a host to ping for the stats view (optional)
    "launch": [],          # Launch button: argv (["npm","run","dev"]) or, to run
                           # steps in order, [["build","npm","run","build"],
                           #                  ["dev","npm","run","dev"]]
    "suites": [],          # [["name","cmd","arg"...], ...] for the Run tests button
    "killPattern": "",     # process-name substring for the Kill button, e.g. "godot"
}
TRACKER_CANDIDATES = ["ROADMAP.md", "ALPHA_RELEASE_TRACKER.md", "docs/ROADMAP.md",
                      "TRACKER.md", "docs/roadmap.md"]


def detect_repo(cwd):
    r = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner",
                        "-q", ".nameWithOwner"], cwd=cwd, capture_output=True,
                       text=True, stdin=subprocess.DEVNULL)
    return r.stdout.strip() if r.returncode == 0 else ""


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.load(open(CONFIG_FILE)))
    except Exception:                                          # noqa: BLE001
        pass
    for key, env in (("repo", "DASHBOARD_REPO"), ("repoDir", "DASHBOARD_REPO_DIR"),
                     ("worktrees", "DASHBOARD_WORKTREES"), ("gameHost", "DASHBOARD_GAME_HOST"),
                     ("tracker", "DASHBOARD_TRACKER")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    cfg["repoDir"] = os.path.abspath(os.path.expanduser(cfg["repoDir"] or os.getcwd()))
    cfg["repo"] = cfg["repo"] or detect_repo(cfg["repoDir"])
    cfg["worktrees"] = os.path.expanduser(cfg["worktrees"] or (cfg["repoDir"] + "-worktrees"))
    if cfg["tracker"] == "auto":
        cfg["tracker"] = next((c for c in TRACKER_CANDIDATES
                               if os.path.exists(os.path.join(cfg["repoDir"], c))), "")
    return cfg


CONFIG = load_config()
REPO = CONFIG["repo"]
REPO_DIR = CONFIG["repoDir"]
WORKTREES = CONFIG["worktrees"]
PORT = int(os.environ.get("PORT", 8787))
FIELDS = "number,title,state,labels,assignees,updatedAt,url,comments,author"
LABEL = "agent-dispatched"
LABEL_COLOR = "8957e5"
TURN_TIMEOUT = 3600
MAX_LINES = 800            # per-task output ring buffer
SAMPLE_EVERY = 15          # seconds between stats samples
MAX_SAMPLES = 5760         # ~24h of history kept on disk

# The stages a build-mode agent moves through, shown as a checklist in the panel.
BUILD_STAGES = ["Worktree", "Investigate", "Implement", "Suites", "Push", "Draft PR"]
PLAN_STAGES = ["Read issue", "Investigate", "Plan"]

# ---------------------------------------------------------------- gh / git


def gh(args, check=False):
    return subprocess.run(["gh", *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, check=check)


def git(args, cwd=REPO_DIR):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)


_ISSUE_CACHE = {}          # (state, fields) -> (fetched_at, payload)
ISSUE_TTL = 20             # seconds; a manual refresh inside this window is free


def gh_issues(state="open", fields=FIELDS, limit="200", force=False):
    key = (state, fields, limit)
    hit = _ISSUE_CACHE.get(key)
    if hit and not force and time.time() - hit[0] < ISSUE_TTL:
        return hit[1]
    r = gh(["issue", "list", "-R", REPO, "--state", state, "--limit", limit,
            "--json", fields], check=True)
    data = json.loads(r.stdout)
    _ISSUE_CACHE[key] = (time.time(), data)
    return data


def gh_issue_body(number):
    r = gh(["issue", "view", str(number), "-R", REPO, "--json", "title,body,url"])
    return json.loads(r.stdout) if r.returncode == 0 else {}


_PRS = {"at": 0, "data": []}


def open_prs():
    """Open PRs, cached briefly, so the issue list can show what is in review."""
    if time.time() - _PRS["at"] < 60:
        return _PRS["data"]
    r = gh(["pr", "list", "-R", REPO, "--state", "open", "--limit", "100",
            "--json", "number,title,url,headRefName,isDraft,body,author"])
    try:
        _PRS.update(at=time.time(), data=json.loads(r.stdout))
    except ValueError:
        pass
    return _PRS["data"]


def prs_for(number):
    """PRs whose branch or body points at this issue."""
    out = []
    for pr in open_prs():
        blob = f"{pr.get('headRefName','')} {pr.get('body') or ''} {pr.get('title','')}"
        if re.search(rf"(issue[-_ ]?{number}\b|#{number}\b)", blob):
            out.append({"number": pr["number"], "url": pr["url"],
                        "draft": pr.get("isDraft", False),
                        "author": (pr.get("author") or {}).get("login")})
    return out


def gh_me():
    r = gh(["api", "user", "--jq", "{login: .login, avatar: .avatar_url, name: .name}"])
    return json.loads(r.stdout) if r.returncode == 0 else {"login": "?"}


def repo_labels():
    r = gh(["label", "list", "-R", REPO, "--limit", "60", "--json", "name"])
    try:
        return [l["name"] for l in json.loads(r.stdout) if l["name"] != LABEL]
    except ValueError:
        return []


def ensure_label():
    gh(["label", "create", LABEL, "-R", REPO, "--color", LABEL_COLOR,
        "--description", "An AI coding agent has been dispatched on this issue"])


def set_label(number, on):
    ensure_label()
    return gh(["issue", "edit", str(number), "-R", REPO,
               "--add-label" if on else "--remove-label", LABEL])


def extract_json(text):
    """Pull the first balanced {...} object out of an agent reply."""
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:                      # this char is escaped; consume it
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str, esc = True, False
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


def create_issue(spec):
    cmd = ["issue", "create", "-R", REPO, "--title", spec["title"], "--body", spec.get("body", "")]
    for lab in spec.get("labels") or []:
        cmd += ["--label", lab]
    r = gh(cmd)
    if r.returncode != 0 and "label" in (r.stderr or "").lower():
        r = gh(["issue", "create", "-R", REPO, "--title", spec["title"],
                "--body", spec.get("body", "")])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "gh issue create failed").strip()[:500])
    return r.stdout.strip().splitlines()[-1]

# ---------------------------------------------------------------- worktrees


def slug(text, n=28):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:n].rstrip("-") or "work"


def ensure_worktree(number, title):
    """`git worktree add` a branch for this issue, per AGENTS.md. Idempotent."""
    branch = f"fix/issue-{number}-{slug(title)}"
    path = os.path.join(WORKTREES, f"issue-{number}")
    if os.path.exists(os.path.join(path, ".git")):
        cur = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path).stdout.strip()
        return {"path": path, "branch": cur or branch, "created": False}
    os.makedirs(WORKTREES, exist_ok=True)
    git(["fetch", "origin", "main"])
    r = git(["worktree", "add", "-b", branch, path, "origin/main"])
    if r.returncode != 0:                       # branch already exists -> reuse it
        r = git(["worktree", "add", path, branch])
        if r.returncode != 0:
            raise RuntimeError((r.stderr or "git worktree add failed").strip()[:400])
    return {"path": path, "branch": branch, "created": True}

# ---------------------------------------------------------------- roadmap

TRACKER = os.path.join(REPO_DIR, CONFIG["tracker"])
STATUS_ORDER = ["Open", "Verification", "Resolved", "Deferred"]
DATE_RE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
ISSUE_RE = re.compile(r"(?:GitHub #|issues/|#)(\d{1,4})\b")


def parse_roadmap():
    if not CONFIG["tracker"]:
        return {"error": "No roadmap tracker configured or found in " + REPO_DIR}
    if not os.path.isfile(TRACKER):
        return {"error": CONFIG["tracker"] + " not found in " + REPO_DIR}
    text = open(TRACKER, encoding="utf-8").read()
    sections, cur, updated, timeline = [], None, None, {}

    def add_events(where, blob):
        for d in set(DATE_RE.findall(blob)):
            timeline.setdefault(d, set()).add(where)

    for raw in text.splitlines():
        line = raw.strip()
        if line.lower().startswith("last updated:"):
            m = DATE_RE.search(line)
            updated = m.group(1) if m else None
            continue
        if line.startswith("## "):
            title = line[3:].replace("?", "·").strip()
            cur = {"title": title, "items": [], "notes": [],
                   "issues": sorted({int(n) for n in ISSUE_RE.findall(title)})}
            sections.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 4 or set(cells[0]) <= set("-: ") or cells[0].lower() == "id":
                continue
            blob = " ".join(cells)
            cur["items"].append({
                "id": cells[0], "severity": cells[1], "status": cells[2],
                "finding": cells[3], "evidence": cells[4] if len(cells) > 4 else "",
                "issues": sorted({int(n) for n in ISSUE_RE.findall(blob)}),
                "dates": sorted(set(DATE_RE.findall(blob)))})
            add_events(cells[0], blob)
        elif line:
            cur["notes"].append(line.lstrip("- ").strip())
            add_events(cur["title"].split("·")[0].strip(), line)

    counts = {k: 0 for k in STATUS_ORDER}
    for sec in sections:
        sec["counts"] = {k: 0 for k in STATUS_ORDER}
        for it in sec["items"]:
            if it["status"] in counts:
                counts[it["status"]] += 1
                sec["counts"][it["status"]] += 1
        sec["issues"] = sorted(set(sec["issues"]) | {n for it in sec["items"] for n in it["issues"]})
    return {"repo": REPO, "updated": updated, "counts": counts,
            "tracked": sum(counts.values()), "done": counts["Resolved"],
            "sections": [s for s in sections if s["items"] or s["notes"]],
            "timeline": [{"date": d, "items": sorted(v)[:10], "n": len(v)}
                         for d, v in sorted(timeline.items())]}

# ---------------------------------------------------------------- config


def save_config(updates):
    """Merge into config.json. Auto-detected values are never written back:
    freezing them would make the next run in another checkout follow this repo."""
    os.makedirs(STATE_DIR, exist_ok=True)
    on_disk = {}
    try:
        on_disk = json.load(open(CONFIG_FILE))
    except Exception:                                          # noqa: BLE001
        pass
    on_disk.update(updates)
    json.dump(on_disk, open(CONFIG_FILE, "w"), indent=1)
    return on_disk


# ---------------------------------------------------------------- stats


def run_quiet(argv, timeout=15):
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        class _T:
            returncode, stdout, stderr = 1, "", "timed out"
        return _T()
    except (OSError, ValueError):
        class _R:
            returncode, stdout, stderr = 1, "", ""
        return _R()


def load_avg():
    """1-minute load where the OS has one; on Windows, CPU busy percent."""
    if hasattr(os, "getloadavg"):
        try:
            return round(os.getloadavg()[0], 2)
        except OSError:
            pass
    out = run_quiet(["powershell", "-NoProfile", "-Command",
                     "(Get-CimInstance Win32_Processor | "
                     "Measure-Object -Property LoadPercentage -Average).Average"]).stdout
    try:
        return round(float(out.strip()), 1)
    except ValueError:
        return None


def mem_used_pct():
    """Used memory as a share of physical memory, per platform."""
    try:
        if WINDOWS:
            out = run_quiet(["powershell", "-NoProfile", "-Command",
                             "$o=Get-CimInstance Win32_OperatingSystem;"
                             "'{0} {1}' -f $o.TotalVisibleMemorySize,$o.FreePhysicalMemory"]).stdout
            total, free = (int(x) for x in out.split())
            return round(100 * (total - free) / total, 1)
        if platform.system() == "Linux":
            info = dict(re.findall(r"^(\w+):\s+(\d+) kB", open("/proc/meminfo").read(), re.M))
            total, avail = int(info["MemTotal"]), int(info["MemAvailable"])
            return round(100 * (total - avail) / total, 1)
        total = int(run_quiet(["sysctl", "-n", "hw.memsize"]).stdout)
        out = run_quiet(["vm_stat"]).stdout
        page = int(re.search(r"page size of (\d+)", out).group(1))
        vals = dict(re.findall(r"^(.*?):\s+(\d+)\.", out, re.M))
        used = sum(int(vals.get(k, 0)) for k in
                   ("Pages wired down", "Pages active", "Pages occupied by compressor")) * page
        return round(100 * used / total, 1)
    except Exception:                                          # noqa: BLE001
        return None


def ping_ms(host):
    if not host:
        return None
    if WINDOWS:
        argv = ["ping", "-n", "1", "-w", "1500", host]
    elif platform.system() == "Linux":
        argv = ["ping", "-c", "1", "-W", "2", host]          # -W is seconds here
    else:
        argv = ["ping", "-c", "1", "-W", "1500", host]       # and milliseconds here
    out = run_quiet(argv, timeout=5).stdout
    m = re.search(r"time[=<]([\d.]+)\s*ms", out)
    return float(m.group(1)) if m else None


def watched_pids():
    """PIDs matching the configured kill pattern (the app this repo runs)."""
    pat = CONFIG.get("killPattern") or ""
    if not pat:
        return []
    if WINDOWS:
        out = run_quiet(["tasklist", "/FO", "CSV", "/NH"]).stdout
        return [int(m.group(2)) for m in re.finditer(r'^"([^"]+)","(\d+)"', out, re.M)
                if pat.lower() in m.group(1).lower()]
    out = run_quiet(["pgrep", "-i", pat]).stdout        # -i, never -f: matching
    return [int(p) for p in out.split() if p.isdigit()]  # full command lines would
                                                         # hit our own agent CLIs


def sample():
    load1 = load_avg()
    with LOCK:
        agents = sum(1 for a in AGENTS.values() if a["busy"])
    with T_LOCK:
        tasks = sum(1 for t in TASKS.values() if t["running"])
    return {"t": int(time.time()), "load": load1, "mem": mem_used_pct(),
            "procs": len(watched_pids()), "agents": agents, "tasks": tasks,
            "ping": ping_ms(CONFIG.get("gameHost"))}


def sampler():
    os.makedirs(STATE_DIR, exist_ok=True)
    while True:
        try:
            with open(STATS_FILE, "a") as f:
                f.write(json.dumps(sample()) + "\n")
            trim_stats()
        except Exception:                                      # noqa: BLE001
            pass
        time.sleep(SAMPLE_EVERY)


def trim_stats():
    try:
        lines = open(STATS_FILE).readlines()
    except OSError:
        return
    if len(lines) > MAX_SAMPLES * 1.2:
        open(STATS_FILE, "w").writelines(lines[-MAX_SAMPLES:])


def read_stats(limit=720):
    try:
        lines = open(STATS_FILE).readlines()[-limit:]
    except OSError:
        return []
    out = []
    for l in lines:
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return out


_ISSUE_HIST = {"at": 0, "data": None}


def issue_history(weeks=12):
    """Weekly opened / closed / open-backlog series straight from GitHub."""
    if _ISSUE_HIST["data"] and time.time() - _ISSUE_HIST["at"] < 300:
        return _ISSUE_HIST["data"]
    try:
        issues = gh_issues("all", "number,createdAt,closedAt,state", "600")
    except subprocess.CalledProcessError:
        return {"weeks": [], "opened": [], "closed": [], "backlog": []}
    now = time.time()
    week = 7 * 86400
    edges = [now - (weeks - i) * week for i in range(weeks + 1)]

    def ts(s):     # GitHub stamps are UTC; mktime would read them as local time
        return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) if s else None

    created = [ts(i["createdAt"]) for i in issues]
    closed = [ts(i.get("closedAt")) for i in issues if i.get("closedAt")]
    opened_s, closed_s, backlog_s, labels = [], [], [], []
    for i in range(weeks):
        lo, hi = edges[i], edges[i + 1]
        opened_s.append(sum(1 for c in created if lo <= c < hi))
        closed_s.append(sum(1 for c in closed if lo <= c < hi))
        backlog_s.append(sum(1 for j, c in enumerate(created) if c < hi) -
                         sum(1 for c in closed if c < hi))
        labels.append(time.strftime("%b %d", time.localtime(lo)).replace(" 0", " "))
    data = {"weeks": labels, "opened": opened_s, "closed": closed_s, "backlog": backlog_s}
    _ISSUE_HIST.update(at=time.time(), data=data)
    return data


def server_status():
    host = CONFIG.get("gameHost")
    st = {"host": host, "ping": ping_ms(host) if host else None}
    st["up"] = st["ping"] is not None
    return st

# ---------------------------------------------------------------- tasks
# Long-running local processes: launch the game, run the suites, kill Godot.

TASKS, T_LOCK = {}, threading.Lock()


def new_task(name, where):
    t = {"id": uuid.uuid4().hex[:8], "name": name, "where": where, "started": int(time.time()),
         "lines": [], "running": True, "exit": None, "proc": None}
    with T_LOCK:
        TASKS[t["id"]] = t
    return t


def emit(t, line):
    with T_LOCK:
        t["lines"].append(line.rstrip("\n"))
        del t["lines"][:-MAX_LINES]


def run_steps(t, steps, cwd):
    """steps: [(label, argv)] run in order; stops at the first failure."""
    code = 0
    try:
        for label, argv in steps:
            emit(t, f"$ {label}: {' '.join(shlex.quote(a) for a in argv)}")
            try:
                p = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     text=True, errors="replace", bufsize=1,
                                     **CHILD_GROUP)
            except OSError as e:
                emit(t, f"[failed to start: {e}]")
                code = 127
                break
            with T_LOCK:
                t["proc"] = p
            try:
                with p.stdout:
                    for line in p.stdout:
                        emit(t, line)
                code = p.wait()
            finally:
                with T_LOCK:
                    t["proc"] = None
            emit(t, f"[{label} exited {code}]")
            if code != 0:
                break
    except Exception as e:                                     # noqa: BLE001
        emit(t, f"[task failed: {e}]")
        code = code or 1
    finally:
        with T_LOCK:
            t["running"], t["exit"] = False, code


def as_steps(spec):
    """Accept either a bare argv or a list of [label, cmd, ...] steps."""
    if not spec:
        return []
    if all(isinstance(x, str) for x in spec):
        return [(spec[0], list(spec))]
    return [(str(s[0]), [str(a) for a in s[1:]]) for s in spec if len(s) > 1]


def missing_binary(steps):
    return next((argv[0] for _, argv in steps if not shutil.which(argv[0])), None)


def start_task(name, steps, cwd):
    t = new_task(name, cwd)
    threading.Thread(target=run_steps, args=(t, steps, cwd), daemon=True).start()
    return t


def kill_watched():
    """Terminate every process matching the configured kill pattern."""
    killed = []
    for pid in watched_pids():
        if pid == os.getpid():
            continue
        try:
            if WINDOWS:
                run_quiet(["taskkill", "/PID", str(pid), "/T", "/F"])
            else:
                os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except OSError:
            pass
    return killed

# ---------------------------------------------------------------- agents

CHILD_GROUP = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS
               else {"start_new_session": True})


def end_process(p):
    """Terminate a child and everything it started."""
    if not p or p.poll() is not None:
        return False
    try:
        if WINDOWS:
            run_quiet(["taskkill", "/PID", str(p.pid), "/T", "/F"], timeout=10)
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    except (OSError, ValueError):
        try:
            p.terminate()
        except OSError:
            return False
    return True


AGENTS, LOCK = {}, threading.Lock()
AGENT_DIR = os.path.join(STATE_DIR, "agents")
VOLATILE = ("proc",)          # everything else about an agent is durable


def save_agent(a):
    """Persist an agent so a restart does not lose the conversation. The CLI
    session id is part of it, so chatting can continue where it left off."""
    try:
        os.makedirs(AGENT_DIR, exist_ok=True)
        tmp = os.path.join(AGENT_DIR, a["id"] + ".tmp")
        with open(tmp, "w") as f:
            json.dump({k: v for k, v in a.items() if k not in VOLATILE}, f)
        os.replace(tmp, os.path.join(AGENT_DIR, a["id"] + ".json"))
    except Exception:                                          # noqa: BLE001
        pass


def forget_agent(a):
    try:
        os.remove(os.path.join(AGENT_DIR, a["id"] + ".json"))
    except OSError:
        pass


def load_agents():
    """Restore agents from disk. A turn that was mid-flight when the server went
    away is reported as interrupted rather than silently dropped."""
    if not os.path.isdir(AGENT_DIR):
        return
    for name in sorted(os.listdir(AGENT_DIR)):
        if not name.endswith(".json"):
            continue
        try:
            a = json.load(open(os.path.join(AGENT_DIR, name)))
        except Exception:                                      # noqa: BLE001
            continue
        if a.get("repo") and a["repo"] != REPO:      # belongs to another project
            continue
        a["proc"] = None
        if a.get("busy"):
            a["messages"].append({"role": "system", "text":
                                  "The dashboard restarted while this turn was running, so it was "
                                  "interrupted. The conversation is intact — send a message to continue."})
        a["busy"], a["stopping"], a["dismissed"] = False, False, False
        AGENTS[a["id"]] = a
        save_agent(a)

PLAN_PROMPT = """You are working in the {repo} repo on GitHub issue #{num}.

Title: {title}
URL: {url}

Body:
{body}

Read the relevant code and give a short assessment: what the issue really is,
where in the repo it lives, and a concrete plan to fix it. Be brief.
Do not modify any files. This is a read-only planning turn."""

BUILD_PROMPT = """You are fixing GitHub issue #{num} in the {repo} repo.

Title: {title}
URL: {url}

Body:
{body}

Your working directory is the git worktree {path}, already checked out on branch
`{branch}` from origin/main. Follow AGENTS.md in the repo root: work only in this
worktree, one issue = one branch = one PR, never push to main, keep tests
committed and runnable, and run every Godot invocation with --headless. Never
open a game window or take a screen grab; captures go through tools/capture.sh.

Do the work, run the six suites AGENTS.md requires and quote their real output,
commit, push the branch, and open a DRAFT pull request that references #{num}
and includes a "Not done" section. Report the PR URL when you are finished."""

NEW_ISSUE_PROMPT = """You are filing a GitHub issue for the {repo} repo from this description:

\"\"\"
{desc}
\"\"\"

Look at the repo enough to make the report concrete and correct (real file paths,
real terminology, existing conventions). Do NOT create the issue yourself and do
NOT modify any files.

Reply with ONLY a JSON object, no prose and no code fence:
{{"title": "<one-line title>", "body": "<markdown body>", "labels": ["existing-label", ...]}}
Existing labels: {labels}. Use only labels from that list, or an empty array."""

# What counts as evidence that a build agent reached a stage, checked against
# everything it has said so far.
STAGE_SIGNS = {
    "Investigate": re.compile(r"\.gd\b|\.tscn\b|res://|file|line \d+", re.I),
    "Implement": re.compile(r"\b(commit|diff|edited|changed|wrote|implement)", re.I),
    "Suites": re.compile(r"self-test|navigation-test|zoom-test|campaign_rules|rival_ai|release_architecture", re.I),
    "Push": re.compile(r"\bpush(ed)?\b|origin/", re.I),
    "Draft PR": re.compile(r"/pull/\d+", re.I),
}


def stages_for(agent):
    """Progress checklist shown at the top of the chat panel.

    Stages are a sequence, so they are reported monotonically: reaching a later
    one means the earlier ones happened, and a failed turn advances nothing."""
    names = BUILD_STAGES if agent["mode"] == "build" else PLAN_STAGES
    if agent["kind"] == "new-issue":
        names = ["Describe", "Research repo", "File issue"]
    replies = [m for m in agent["messages"] if m["role"] not in ("user", "system")]
    failed = bool(replies) and replies[-1]["role"] == "error"
    blob = "\n".join(m["text"] for m in replies if m["role"] != "error")
    answered = any(m["role"] not in ("error",) for m in replies)

    done = []
    for n in names:
        if n == "Worktree":
            done.append(bool(agent.get("worktree")))
        elif n in ("Read issue", "Describe"):
            done.append(True)
        elif n == "Draft PR":
            done.append(bool(agent.get("pr")))
        elif n == "File issue":
            done.append(bool(agent["issue"].get("url")))
        elif n in ("Plan", "Research repo"):
            done.append(answered and not agent["busy"] and not failed)
        else:
            done.append(bool(STAGE_SIGNS[n].search(blob)) if n in STAGE_SIGNS else False)

    last = max((i for i, d in enumerate(done) if d), default=-1)   # fill the gaps
    done = [i <= last for i in range(len(done))]
    cur = next((i for i, d in enumerate(done) if not d), None)
    return [{"name": n, "done": d, "current": agent["busy"] and i == cur,
             "failed": failed and i == cur}
            for i, (n, d) in enumerate(zip(names, done))]


def spawn(agent, cmd, cwd):
    if agent.get("dismissed"):
        raise RuntimeError("agent was dismissed")
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         stdin=subprocess.DEVNULL, text=True, errors="replace",
                         **CHILD_GROUP)
    agent["proc"] = p
    try:
        out, err = p.communicate(timeout=TURN_TIMEOUT)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise
    finally:
        agent["proc"] = None
    return p.returncode, out, err


def take_attachments(agent):
    """Hand the pending drops to this turn and clear them."""
    with LOCK:
        imgs, agent["images"] = agent.get("images", []), []
        files, agent["files"] = agent.get("files", []), []
    return imgs, files


def run_claude(agent, text):
    imgs, files = take_attachments(agent)
    if imgs or files:
        text += "\n\nAttached by the user — read each one with the Read tool:\n" + \
                "\n".join(imgs + files)
    cmd = ["claude", "-p", text, "--output-format", "json"]
    cmd += (["--permission-mode", "bypassPermissions"] if agent["mode"] == "build"
            else ["--permission-mode", "plan"])
    if agent["model"]:
        cmd += ["--model", agent["model"]]
    if agent["session"]:
        cmd += ["--resume", agent["session"]]
    code, out, err = spawn(agent, cmd, agent["cwd"])
    if not out.strip():
        raise RuntimeError((err or f"claude exited {code}").strip()[:2000])
    payload = json.loads(out)
    agent["session"] = payload.get("session_id") or agent["session"]
    text = payload.get("result") or "(no output)"
    if payload.get("is_error") or code != 0:
        # the CLI reports a failed turn in-band (bad model, auth, limits): it is
        # an error, not an answer, and must not advance the agent's stages
        raise RuntimeError(text.strip()[:2000])
    return text


def run_codex(agent, text):
    base = ["codex", "exec", "--json", "-C", agent["cwd"], "--skip-git-repo-check"]
    imgs, files = take_attachments(agent)
    for img in imgs:                       # codex takes images as real attachments
        base += ["-i", img]
    if files:                              # anything else goes across as a path to read
        text += "\n\nAttached by the user — read each one:\n" + "\n".join(files)
    base += (["-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"]
             if agent["mode"] == "build" else ["-s", "read-only"])
    if agent["model"]:
        base += ["-m", agent["model"]]
    cmd = base + (["resume", agent["session"], text] if agent["session"] else [text])
    code, out, err = spawn(agent, cmd, agent["cwd"])
    reply, evt_err = [], None
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "thread.started" and ev.get("thread_id"):
            agent["session"] = ev["thread_id"]
        item = ev.get("item") or {}
        if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
            reply.append(item.get("text", ""))
        if ev.get("type") == "error":
            evt_err = ev.get("message")
    if reply:
        return "\n\n".join(reply)
    raise RuntimeError((evt_err or err or f"codex exited {code}").strip()[:2000] or "(no output)")


RUNNERS = {"claude": run_claude, "codex": run_codex}


def worker(agent, text):
    try:
        reply, role = RUNNERS[agent["provider"]](agent, text), agent["provider"]
    except subprocess.TimeoutExpired:
        reply, role = f"Turn timed out after {TURN_TIMEOUT}s.", "error"
    except Exception as e:                                     # noqa: BLE001
        reply, role = ("Stopped." if agent.get("stopping") else str(e)), "error"
    extra = None
    if role != "error" and agent["kind"] == "new-issue":
        spec = extract_json(reply)
        if spec and spec.get("title"):
            try:
                url = create_issue(spec)
                agent["kind"] = "chat"
                agent["issue"].update({"url": url, "title": spec["title"],
                                       "number": url.rstrip("/").split("/")[-1]})
                reply = spec["title"] + "\n\n" + (spec.get("body") or "")
                extra = {"role": "system", "text": "Created " + url}
            except Exception as e:                             # noqa: BLE001
                extra = {"role": "error", "text": f"Could not create the issue: {e}"}
        else:
            extra = {"role": "system", "text":
                     "No JSON issue spec in that reply. Ask the agent to reply with only the JSON object."}
    if role != "error" and not agent.get("pr"):
        m = re.search(r"https://github\.com/\S+?/pull/\d+", reply)
        if m:
            agent["pr"] = m.group(0).rstrip(").,")
    with LOCK:
        agent["stopping"] = False
        agent["messages"].append({"role": role, "text": reply})
        if extra:
            agent["messages"].append(extra)
        agent["busy"] = False
    save_agent(agent)


def enqueue(agent, text, show_user=True):
    with LOCK:
        if agent["busy"]:
            return False
        if show_user:
            agent["messages"].append({"role": "user", "text": text})
        agent["busy"] = True
    save_agent(agent)
    threading.Thread(target=worker, args=(agent, text), daemon=True).start()
    return True


def stop_agent(agent):
    """Kill the running turn (and its children). Only then call it a stop."""
    if end_process(agent.get("proc")):
        agent["stopping"] = True
        return True
    return False


def new_agent(**kw):
    a = {"id": uuid.uuid4().hex[:8], "session": None, "busy": False, "stopping": False,
         "proc": None, "kind": "chat", "mode": "plan", "model": "", "messages": [],
         "cwd": REPO_DIR, "worktree": None, "branch": None, "pr": None,
         "images": [], "files": [],
         "started": int(time.time()), "repo": REPO, **kw}
    with LOCK:
        AGENTS[a["id"]] = a
    save_agent(a)
    return a


def summary(a):
    return {k: a[k] for k in ("id", "issue", "provider", "model", "mode", "kind",
                              "busy", "worktree", "branch", "pr", "started")}


def public(a):
    return {**summary(a), "messages": a["messages"], "stages": stages_for(a),
            "pendingImages": len(a.get("images", [])) + len(a.get("files", []))}

# ---------------------------------------------------------------- http


def guarded(fn):
    """A handler that raises must still answer, or the client hangs on keep-alive."""
    def wrapper(self):
        try:
            fn(self)
        except Exception as e:                                 # noqa: BLE001
            try:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:                                  # noqa: BLE001
                pass
    return wrapper


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json", json.dumps(obj).encode())

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _part(self, i):
        return self.path.split("?")[0].split("/")[i]

    # ---- GET
    @guarded
    def do_GET(self):
        path, _, query = self.path.partition("?")
        q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self._send(200, "text/html; charset=utf-8", f.read())
        elif path == "/api/me":
            self._json({**gh_me(), "repo": REPO,
                        "repoName": REPO.split("/")[-1], "config": CONFIG})
        elif path == "/api/roadmap":
            self._json(parse_roadmap())
        elif path == "/api/stats":
            try:
                n = max(1, min(int(q.get("n", 240)), MAX_SAMPLES))
            except ValueError:
                n = 240
            self._json({"now": sample(), "history": read_stats(n),
                        "issues": issue_history(), "server": server_status(),
                        "pids": watched_pids(), "config": CONFIG,
                        "killPattern": CONFIG.get("killPattern", "")})
        elif path == "/api/issues":
            state = q.get("state", "open")
            if state not in ("open", "closed", "all"):
                state = "open"
            try:                     # copy: the cached payload must stay unenriched
                issues = json.loads(json.dumps(gh_issues(state)))
            except subprocess.CalledProcessError as e:
                return self._json({"error": e.stderr}, 500)
            with LOCK:
                live = {str(a["issue"].get("number")): a["id"] for a in AGENTS.values()}
            with LOCK:
                busy = {str(a["issue"].get("number")) for a in AGENTS.values() if a["busy"]}
            for i in issues:
                num = str(i["number"])
                i["agentId"] = live.get(num)
                i["dispatched"] = any(l["name"] == LABEL for l in i["labels"])
                i["labels"] = [l for l in i["labels"] if l["name"] != LABEL]
                i["prs"] = prs_for(i["number"])
                i["claimedBy"] = [a["login"] for a in i.get("assignees") or []]
                if i["state"] == "CLOSED":
                    i["status"] = "closed"
                elif i["prs"]:
                    i["status"] = "in review"
                elif num in busy:
                    i["status"] = "agent working"
                elif i["agentId"] or i["dispatched"]:
                    i["status"] = "agent dispatched"
                elif i["claimedBy"]:
                    i["status"] = "claimed"
                else:
                    i["status"] = "todo"
            self._json(issues)
        elif path == "/api/agents":
            with LOCK:
                self._json([summary(a) for a in AGENTS.values()])
        elif path.startswith("/api/agents/"):
            a = AGENTS.get(self._part(3))
            self._json(public(a) if a else {"error": "no such agent"}, 200 if a else 404)
        elif path == "/api/tasks":
            with T_LOCK:
                self._json([{"id": t["id"], "name": t["name"], "running": t["running"],
                             "exit": t["exit"], "started": t["started"]}
                            for t in TASKS.values()])
        elif path.startswith("/api/tasks/"):
            t = TASKS.get(self._part(3))
            if not t:
                return self._json({"error": "no such task"}, 404)
            with T_LOCK:
                payload = {"id": t["id"], "name": t["name"], "where": t["where"],
                           "running": t["running"], "exit": t["exit"],
                           "output": "\n".join(t["lines"])}
            self._json(payload)
        else:
            self._send(404, "text/plain", b"not found")

    # ---- POST
    @guarded
    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/config":
            updates = {k: v for k, v in self._body().items() if k in DEFAULTS}
            CONFIG.update(updates)
            save_config(updates)
            return self._json(CONFIG)

        if path == "/api/agents":
            b = self._body()
            provider, mode = b.get("provider", "claude"), b.get("mode", "plan")
            if provider not in RUNNERS:
                return self._json({"error": f"unknown provider {provider}"}, 400)
            if not shutil.which(provider):
                return self._json({"error": f"{provider} CLI not found on PATH"}, 400)
            number = b.get("number")
            issue = {"number": number, "title": b.get("title"), "url": b.get("url")}
            issue.update({k: v for k, v in gh_issue_body(number).items()
                          if k in ("title", "body", "url")})
            wt = None
            if mode == "build":
                try:
                    wt = ensure_worktree(number, issue.get("title"))
                except Exception as e:                          # noqa: BLE001
                    return self._json({"error": f"worktree: {e}"}, 500)
            agent = new_agent(issue=issue, provider=provider, mode=mode,
                              model=(b.get("model") or "").strip(),
                              cwd=wt["path"] if wt else REPO_DIR,
                              worktree=wt["path"] if wt else None,
                              branch=wt["branch"] if wt else None)
            lab = set_label(number, True)
            note = f"Dispatched {provider} on #{number} in {mode} mode."
            note += ("\nLabeled `%s` on GitHub." % LABEL if lab.returncode == 0
                     else "\nCould not apply the GitHub label: " + (lab.stderr or "").strip()[:200])
            if wt:
                note += f"\nWorktree {wt['path']}\nBranch {wt['branch']}"
            agent["messages"].append({"role": "system", "text": note})
            save_agent(agent)
            prompt = (BUILD_PROMPT if mode == "build" else PLAN_PROMPT).format(
                repo=REPO, num=number, title=issue.get("title", ""),
                url=issue.get("url", ""), body=(issue.get("body") or "(no body)")[:6000],
                path=wt["path"] if wt else REPO_DIR, branch=wt["branch"] if wt else "main")
            enqueue(agent, prompt, show_user=False)
            return self._json({"id": agent["id"]})

        if path == "/api/issues/new":
            b = self._body()
            provider, desc = b.get("provider", "claude"), (b.get("text") or "").strip()
            if provider not in RUNNERS:
                return self._json({"error": f"unknown provider {provider}"}, 400)
            if not shutil.which(provider):
                return self._json({"error": f"{provider} CLI not found on PATH"}, 400)
            if not desc:
                return self._json({"error": "describe the issue first"}, 400)
            agent = new_agent(issue={"number": None, "title": desc[:80], "url": None},
                              provider=provider, model=(b.get("model") or "").strip(),
                              kind="new-issue", messages=[{"role": "user", "text": desc}])
            enqueue(agent, NEW_ISSUE_PROMPT.format(repo=REPO, desc=desc,
                                                   labels=", ".join(repo_labels())),
                    show_user=False)
            return self._json({"id": agent["id"]})

        if path == "/api/tasks":
            b = self._body()
            kind = b.get("kind")
            agent = AGENTS.get(b.get("agent") or "")
            cwd = agent["worktree"] if (agent and agent.get("worktree")) else REPO_DIR
            where = agent["branch"] if (agent and agent.get("branch")) else "main checkout"
            if kind == "kill":
                if not CONFIG.get("killPattern"):
                    return self._json({"error": "no killPattern configured"}, 400)
                killed = kill_watched()
                t = new_task("kill " + CONFIG["killPattern"], "machine")
                emit(t, f"Terminated {len(killed)} process(es) matching "
                        f"{CONFIG['killPattern']!r}: "
                        + (", ".join(map(str, killed)) or "none were running"))
                with T_LOCK:
                    t["running"], t["exit"] = False, 0
                return self._json({"id": t["id"]})
            if kind == "launch":
                steps = as_steps(CONFIG["launch"])
                if not steps:
                    return self._json({"error": "no launch command configured"}, 400)
                missing = missing_binary(steps)
                if missing:
                    return self._json({"error": f"{missing} not on PATH"}, 400)
                t = start_task(f"launch · {where}", steps, cwd)
                return self._json({"id": t["id"]})
            if kind == "tests":
                steps = as_steps(CONFIG["suites"])
                if not steps:
                    return self._json({"error": "no suites configured"}, 400)
                missing = missing_binary(steps)
                if missing:
                    return self._json({"error": f"{missing} not on PATH"}, 400)
                t = start_task(f"{len(steps)} step{'s' if len(steps) > 1 else ''} · {where}",
                               steps, cwd)
                return self._json({"id": t["id"]})
            return self._json({"error": f"unknown task {kind}"}, 400)

        if path.startswith("/api/tasks/") and path.endswith("/stop"):
            t = TASKS.get(self._part(3))
            if not t:
                return self._json({"error": "no such task"}, 404)
            with T_LOCK:
                p = t.get("proc")
            return self._json({"ok": end_process(p)})

        if path.startswith("/api/agents/"):
            a = AGENTS.get(self._part(3))
            if not a:
                return self._json({"error": "no such agent"}, 404)
            if path.endswith("/message"):
                text = (self._body().get("text") or "").strip()
                if not text:
                    return self._json({"error": "empty message"}, 400)
                if not enqueue(a, text):
                    return self._json({"error": "agent busy"}, 409)
                return self._json({"ok": True})
            if path.endswith("/stop"):
                return self._json({"stopped": stop_agent(a)})
            if path.endswith("/attach"):
                b = self._body()
                head, _, payload = (b.get("dataUrl") or "").partition(",")
                if not payload:
                    return self._json({"error": "expected a data: URL"}, 400)
                is_image = head.startswith("data:image/")
                given = os.path.basename(b.get("name") or "")
                given = re.sub(r"[^A-Za-z0-9._-]", "_", given)[:60]
                if not given:
                    ext = re.search(r"data:image/(\w+)", head)
                    given = f"paste.{ext.group(1) if ext else 'png'}"
                name = f"{a['id']}-{uuid.uuid4().hex[:6]}-{given}"
                os.makedirs(os.path.join(STATE_DIR, "uploads"), exist_ok=True)
                dest = os.path.join(STATE_DIR, "uploads", name)
                import base64
                try:
                    open(dest, "wb").write(base64.b64decode(payload))
                except Exception:                              # noqa: BLE001
                    return self._json({"error": "could not decode that file"}, 400)
                with LOCK:
                    a.setdefault("images" if is_image else "files", []).append(dest)
                    pending = len(a.get("images", [])) + len(a.get("files", []))
                save_agent(a)
                return self._json({"path": dest, "image": is_image, "pending": pending})
        self._send(404, "text/plain", b"not found")

    # ---- DELETE: dismiss an agent, clear its label (worktree is left on disk)
    @guarded
    def do_DELETE(self):
        if self.path.startswith("/api/agents/"):
            a = AGENTS.get(self._part(3))
            if not a:
                return self._json({"error": "no such agent"}, 404)
            a["dismissed"] = True          # blocks a queued turn from spawning
            stop_agent(a)
            with LOCK:
                AGENTS.pop(a["id"], None)
            forget_agent(a)
            cleared = bool(a["issue"].get("number")) and \
                set_label(a["issue"]["number"], False).returncode == 0
            return self._json({"ok": True, "labelCleared": cleared,
                               "worktree": a.get("worktree")})
        self._send(404, "text/plain", b"not found")


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    load_agents()
    if AGENTS:
        print(f"restored {len(AGENTS)} agent(s) from {AGENT_DIR}")
    threading.Thread(target=sampler, daemon=True).start()
    with Server(("127.0.0.1", PORT), Handler) as s:
        print(f"sword dashboard: http://localhost:{PORT}")
        try:
            s.serve_forever()
        except KeyboardInterrupt:
            sys.exit(0)
