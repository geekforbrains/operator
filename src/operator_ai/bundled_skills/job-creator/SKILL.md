---
name: job-creator
description: >-
  Creates, updates, and manages scheduled jobs using the manage_job tool.
  Use when the user wants to automate a recurring task — daily summaries,
  monitors, alerts, syncs, digests, periodic reports, or any cron-scheduled
  workflow. Covers job file anatomy, cron schedules, prompt writing, stateful
  KV patterns, hooks, and the critical send_message delivery rule.
metadata:
  author: operator
  version: "1.0"
---

# Job Creator

Create well-formed scheduled jobs using `manage_job`. Jobs are cron-scheduled agent conversations
that run autonomously using the selected agent's prompt, tools, skills, and permissions.

## When to Create a Job

**Create a job when** the task recurs on a schedule, should run without human prompting, and
needs to deliver output to a channel automatically.

**Don't create a job when** it's a one-off task, is interactive/conversational, or has no
clear recurring schedule.

## How Jobs Work

1. The job runner ticks every 60s, scanning `$OPERATOR_HOME/jobs/*.md`
2. If a job's cron matches the current minute and it's enabled, it fires
3. If the job is already running, the tick is skipped (tracked via `skip_count`)
4. Optional `prerun` hook can gate execution (non-zero exit = skip, tracked via `gate_count`)
5. A fresh agent conversation starts — the job body becomes the user message
6. The agent runs with the selected agent's prompt, tools, skills, sandbox, and permissions
7. Optional `postrun` hook receives agent output on stdin
8. Job state (last_run, result, duration, error, counts) is persisted in SQLite

### Critical Facts

- **Each run starts a fresh conversation** — prompt state does not carry over automatically
- **The workspace persists** — files written to disk survive across runs
- **KV store persists** — use it for cross-run state (scoped per agent, not per job)
- **Agent/global memory can persist** when memory is enabled — useful for durable facts, not operational cursors
- **Text responses go NOWHERE** — see the send_message rule below

---

## The send_message Rule

**Job output not explicitly sent to a channel is LOST.** The agent's text responses are not
delivered anywhere. Every job prompt MUST specify:

1. Which channel(s) to post to (by name)
2. What format the message should take
3. Whether to use threading (post teaser, reply in thread with details)

---

## Job File Anatomy

```markdown
---
name: my-job-name
description: What this job does
schedule: "0 8 * * *"
max_iterations: 10
enabled: true
hooks:
  prerun: scripts/check.sh
  postrun: scripts/notify.sh
---

The prompt body — becomes the user message. Be explicit about what to do and where to post.
```

### Frontmatter Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | No | filename stem | Display name |
| `description` | No | — | Human-readable summary |
| `schedule` | **Yes** | — | Cron expression (validated by croniter) |
| `agent` | No | default agent | Which agent runs it |
| `max_iterations` | No | agent default | Override for complex multi-step jobs |
| `enabled` | No | `true` | Set `false` to pause without deleting |
| `hooks.prerun` | No | — | Gate script (non-zero exit = skip) |
| `hooks.postrun` | No | — | Receives agent output on stdin |

## Cron Schedule Reference

Format: `minute hour day-of-month month day-of-week`

```
"*/15 * * * *"       # Every 15 minutes
"*/30 * * * *"       # Every 30 minutes
"0 */2 * * *"        # Every 2 hours
"0 */6 * * *"        # Every 6 hours
"0 8 * * *"          # Daily at 8 AM
"0 8 * * 1-5"        # Weekdays at 8 AM
"0 9,17 * * *"       # 9 AM and 5 PM daily
"30 8 * * 1"         # Mondays at 8:30 AM
"0 0 * * 0"          # Weekly on Sunday midnight
"0 0 1 * *"          # Monthly on the 1st
```

Tips: minute-level is finest granularity, cron schedules run in UTC, avoid too-frequent
schedules (each run is a full agent conversation with LLM calls).

---

## Prompt Writing Patterns

### Explicit Channel Targeting — always name the channel

```
Post a summary to #general with the top 10 stories as a bulleted list.
```

### Threading — teaser + threaded details

```
Post a one-line teaser to #general. Then reply in a thread on that message with the full breakdown.
```

The agent posts via `send_message`, gets back a message ID, then passes it as `thread_id`.

### Conditional Posting — only post when there's news

```
If any workflow runs failed in the last hour, post an alert to #dev.
If all runs are passing, do NOT post anything. Stay silent.
```

### Stateful with KV — track state across runs

```
Use KV namespace "blog-monitor" to track seen URLs:
- Get key "seen-urls" for previously posted URLs (JSON array)
- Only post NEW articles not in that list
- Update "seen-urls" after processing (ttl_hours=720)
```

### Sub-Agents — parallelize complex work

```
Spawn sub-agents to check GitHub PRs, Issues, and CI status in parallel.
Combine results into a single report and post to #dev.
```

---

## KV Store Patterns

Jobs start fresh each run, but KV persists. **Always use the job name as namespace.**

### Deduplication

```
kv_get(namespace="rss-monitor", key="seen-ids") -> JSON array
# Process only new items, then:
kv_set(namespace="rss-monitor", key="seen-ids", value=<updated JSON>, ttl_hours=720)
```

### Cursors / Watermarks

```
kv_get(namespace="github-digest", key="last-checked") -> ISO timestamp or [not found]
# Fetch items since that timestamp, then:
kv_set(namespace="github-digest", key="last-checked", value=<now>)
```

### State Transitions

```
kv_get(namespace="deploy-watcher", key="last-status") -> "passing" | "failing"
# Only alert on TRANSITIONS (passing->failing), not repeated failures
kv_set(namespace="deploy-watcher", key="last-status", value=<new status>)
```

### KV Tips

- Keys and values are strings — use JSON for structured data
- `ttl_hours` auto-expires keys — great for dedup windows (168h = 7 days, 720h = 30 days)
- KV is scoped per-agent, not per-job — namespace prevents collisions
- `kv_list(namespace="job-name")` to inspect stored state

---

## Workspace Persistence

The agent workspace persists across runs. Use for cache files, helper scripts, templates, or
large state that doesn't fit well in KV. Example:

```
Check if workspace/cache/feed.json exists and is less than 1 hour old.
If so, use cached. Otherwise, fetch fresh and save.
```

---

## Hooks

### Prerun (Gating)

Non-zero exit = skip this run. `manage_job` auto-creates placeholder scripts.

### Postrun (Processing)

Receives agent output on stdin. Good for logging or downstream triggers.

### Hook Environment

Every hook script receives these environment variables:

| Variable | Description |
|----------|-------------|
| `JOB_NAME` | Name of the job being executed |
| `OPERATOR_AGENT` | Agent running the job |
| `OPERATOR_HOME` | Operator base directory |
| `OPERATOR_DB` | Path to the SQLite database |

### Hook Examples

```bash
#!/bin/bash
# scripts/check-cooldown.sh — prerun: skip if we ran recently (KV TTL gate)
operator kv get cooldown --ns "$JOB_NAME" >/dev/null 2>&1 && exit 1
exit 0
# Tip: the job prompt sets `kv_set("cooldown", "1", namespace=JOB_NAME, ttl_hours=6)`
# so this gate auto-clears after 6 hours.
```

```bash
#!/bin/bash
# scripts/weekday-only.sh — prerun: skip weekends
DOW=$(date +%u)
[ "$DOW" -gt 5 ] && exit 1
exit 0
```

```bash
#!/bin/bash
# scripts/log-output.sh — postrun: log agent output and store in KV
OUTPUT=$(cat)
echo "[$(date -Iseconds)] $OUTPUT" >> $OPERATOR_HOME/logs/job-output.log
operator kv set last-output "$OUTPUT" --ns "$JOB_NAME" --ttl 168
```

---

## Job Archetypes

### 1. Digest / Summary

```markdown
---
name: hackernews-morning
description: Daily Hacker News summary
schedule: "0 8 * * *"
enabled: true
---
Fetch the Hacker News front page and summarize the top 10-15 stories.

Post a one-line teaser to #general, then reply in a thread with the full breakdown
organized by: Tech, Industry, AI/ML, Security, Notable discussions.
Keep concise. Skip job postings and Show HN self-promos.
```

### 2. Monitor / Alert (Stateful)

```markdown
---
name: ci-failure-alert
description: Alert on GitHub Actions CI failures
schedule: "*/15 * * * *"
max_iterations: 5
enabled: true
---
Check GitHub Actions for failed workflow runs.

Use KV namespace "ci-failure-alert":
- Get "last-checked" for previous check timestamp
- Get "alerted-runs" for JSON array of already-alerted run IDs

For NEW failures not in alerted-runs:
- Post alert to #dev with repo, workflow, branch, error summary, and link

Update "last-checked" to now. Update "alerted-runs" (ttl_hours=168).
If no new failures, stay silent.
```

### 3. Sync / Import

```markdown
---
name: rss-research-feed
description: Post new articles from research RSS feeds
schedule: "0 */4 * * *"
enabled: true
---
Check these RSS feeds for new articles:
- https://arxiv.org/rss/cs.AI
- https://blog.openai.com/rss/

Use KV namespace "rss-research-feed":
- Get "seen-urls" -> JSON array of already-posted URLs (default: [])

Post each NEW article to #research with title, one-sentence summary, and link.
Update "seen-urls" after processing (ttl_hours=720). Stay silent if nothing new.
```

### 4. Periodic Cleanup

```markdown
---
name: workspace-cleanup
description: Weekly workspace and temp file cleanup
schedule: "0 3 * * 0"
enabled: true
---
Weekly maintenance:
1. Delete files older than 30 days in workspace/cache/
2. Check disk usage of $OPERATOR_HOME/

Post a brief summary to #background with what was cleaned and disk usage.
```

### 5. Scheduled Report

```markdown
---
name: weekly-project-report
description: Monday morning project status report
schedule: "0 9 * * 1"
max_iterations: 15
enabled: true
---
Compile a weekly project status report for the past 7 days.

Gather: GitHub PRs merged, issues opened/closed, stale PRs (open >7 days, no review).

Post summary to #dev. Thread the detailed report.
```

---

## Anti-Patterns

| Bad | Good | Why |
|-----|------|-----|
| `Summarize the news and give me a report.` | `Summarize the news and post to #general.` | Output goes nowhere without send_message |
| `Post the results to Slack.` | `Post the results to #dev.` | Must specify which channel |
| `Check for errors and post status to #dev.` | `Only post to #dev if errors found. Stay silent otherwise.` | Avoid spamming on no-op runs |
| `Post new articles to #research.` | `Use KV "my-job"/"seen-urls" to track posted articles. Only post new ones.` | Dedup prevents re-posting |
| `schedule: "* * * * *"` (heavy job) | `schedule: "0 */6 * * *"` | Each run costs LLM calls |
| `Last count was 42. Check if changed.` | `Get count from KV "my-counter"/"last-count". Compare. Store new.` | State belongs in KV, not prompt |
| Complex multi-source job, default iterations | Same job with `max_iterations: 15` | Complex jobs need headroom |

---

## Using manage_job

### Create

```python
manage_job(
    action="create",
    name="my-job",
    config="""---
name: my-job
description: What it does
schedule: "0 8 * * *"
enabled: true
---
The prompt body with explicit channel targeting and delivery instructions.
"""
)
```

### Other Actions

```python
manage_job(action="list")                          # List all jobs with status
manage_job(action="update", name="my-job", config="...")  # Replace job file content
manage_job(action="enable", name="my-job")         # Re-enable a disabled job
manage_job(action="disable", name="my-job")        # Pause without deleting
manage_job(action="delete", name="my-job")         # Remove entirely
```

### What manage_job Validates

- Cron schedule is valid (croniter)
- Agent name exists in config
- Hooks field is a dict (not a list)
- Creates placeholder hook scripts if referenced

---

## Pre-Creation Checklist

- [ ] Has a clear recurring schedule (not a one-off task)
- [ ] `schedule` is a valid cron expression
- [ ] Prompt explicitly names target channel(s) for `send_message`
- [ ] Prompt specifies message format (threading, conditional, etc.)
- [ ] Stateful jobs use KV with job name as namespace
- [ ] KV keys have appropriate TTL to prevent unbounded growth
- [ ] `max_iterations` set if job needs many tool calls (>10 steps)
- [ ] `agent` field set if it should run as a specific agent
- [ ] Description is clear and concise
- [ ] Filename matches `name` in frontmatter
