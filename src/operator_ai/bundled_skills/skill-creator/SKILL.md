---
name: skill-creator
description: >-
  Creates, updates, and manages Operator skills — reusable instruction documents
  that teach agents new capabilities. Use when the user asks to learn something,
  create a skill, improve an existing skill, or make the agent smarter.
metadata:
  author: operator
  version: "1.0"
---

# Skill Creator

## When to create a skill

Create a skill when the user:
- Says "learn to...", "remember how to...", "always do X when..."
- Asks you to create or update a skill explicitly
- Wants to codify a workflow they'll reuse
- Completed a complex task well and wants to repeat it reliably

Update an existing skill (don't create a duplicate) when:
- The user says a skill could be better or is wrong
- You notice a skill's instructions are incomplete after using it

## How skills work

Skills follow the [agentskills.io](https://agentskills.io) spec. A skill is a directory containing a `SKILL.md` file with YAML frontmatter and markdown instructions:

```
my-skill/
├── SKILL.md           # Required: metadata + instructions
├── scripts/           # Optional: executable code
├── references/        # Optional: detailed docs loaded on demand
└── assets/            # Optional: templates, data files
```

**Progressive disclosure:** Only lightweight skill metadata is loaded into the system prompt at startup. The full `SKILL.md` body is loaded when the skill activates. Files in `scripts/`, `references/`, and `assets/` are loaded only when explicitly read. This keeps context efficient.

## Creating a skill

Use the `manage_skill` tool:

```
manage_skill(action="create", name="my-skill", config="<full SKILL.md content>")
```

The `config` argument is the complete SKILL.md file content including frontmatter.

## Frontmatter rules

Required fields:
- **`name`**: Lowercase alphanumeric + hyphens. 1-64 chars. Must match the directory name. No leading/trailing/consecutive hyphens.
- **`description`**: 1-1024 chars. Write in **third person**. Include what it does AND when to use it. Be specific — this is how the agent decides to activate the skill.

Optional fields: `metadata`, plus informational fields like `license` or `compatibility`. Operator actively checks `metadata.env`; the rest are descriptive unless your own workflow uses them. See [references/frontmatter-reference.md](references/frontmatter-reference.md) for details.

### Good description

```yaml
description: >-
  Generates weekly analytics reports from PostgreSQL, formats as markdown
  tables, and posts to Slack. Use when asked for analytics, weekly reports,
  or database summaries.
```

### Bad description

```yaml
description: Helps with reports.
```

## Writing skill instructions

The markdown body after frontmatter is the actual instructions. Follow these rules:

### Be concise — the agent is smart
Only add context the agent doesn't already have. Don't explain what PDFs are or how databases work. Focus on the specific knowledge, preferences, and patterns unique to this skill.

### One capability per skill
Don't combine "deploy to AWS" and "write unit tests" in one skill. Keep skills focused so they activate precisely.

### Structure for clarity
Use headers to organize: Purpose, When to Use, Steps, Examples, Common Mistakes.

### Keep under 500 lines
If longer, split detailed reference material into `references/` files and link to them from SKILL.md. The agent will read those files on demand.

### Use file references for large content
```markdown
For the full API schema, see [references/schema.md](references/schema.md).
Run the validation script: `scripts/validate.sh`
```

Keep references one level deep from SKILL.md — avoid chains of files referencing other files.

## Critical rules

### NEVER hardcode secrets
Never put API keys, tokens, passwords, or credentials in SKILL.md. Instead:

1. Reference env vars by name in your instructions:
   ```markdown
   Authenticate using `$GITHUB_TOKEN` in shell commands.
   ```

2. List required env vars in frontmatter so Operator can warn when they are missing:
   ```yaml
   metadata:
     env:
       - GITHUB_TOKEN
       - SLACK_WEBHOOK_URL
   ```

3. The user adds actual values to their `.env` file (configured via `runtime.env_file` in `operator.yaml`).

### ALWAYS use $OPERATOR_HOME for paths
When referencing files under the Operator base directory in skill instructions or scripts, always use `$OPERATOR_HOME` instead of `~/.operator`. Tilde expansion is a shell feature and breaks in non-shell contexts (e.g. `node ~/...` treats `~` as a literal directory name). `$OPERATOR_HOME` is set by the runtime and expands correctly everywhere.

```markdown
# Good
node $OPERATOR_HOME/skills/my-skill/scripts/run.js

# Bad — tilde may not expand
node ~/.operator/skills/my-skill/scripts/run.js
```

### NEVER duplicate built-in tools
Skills add knowledge and procedures, not functionality. Don't write a skill that reimplements `run_shell`, `read_file`, `write_file`, `send_message`, etc.

### ALWAYS write descriptions in third person
- Good: "Generates deployment manifests..."
- Bad: "I generate deployment manifests..." or "You can use this to..."

## Example: complete skill

```markdown
---
name: pr-reviewer
description: >-
  Reviews GitHub pull requests by analyzing diffs, checking for common issues,
  and posting structured review comments. Use when asked to review a PR, check
  code quality, or provide feedback on changes.
metadata:
  env:
    - GITHUB_TOKEN
---

# PR Reviewer

## Steps

1. Fetch the PR diff using `gh pr diff <number>`
2. Analyze for:
   - Security issues (hardcoded secrets, SQL injection, XSS)
   - Logic errors and edge cases
   - Style consistency with the existing codebase
   - Missing tests for new functionality
3. Post a structured review using `gh pr review <number>`

## Review format

Use this structure for review comments:

### Summary
One paragraph overview of the changes.

### Issues
Bulleted list, severity-tagged: `[critical]`, `[suggestion]`, `[nit]`.

### Verdict
APPROVE, REQUEST_CHANGES, or COMMENT with a one-line rationale.
```

## Skill lifecycle

1. **Create**: `manage_skill(action="create", name="...", config="...")`
2. **Test**: Ask the user to try the skill on a real task
3. **Iterate**: `manage_skill(action="update", name="...", config="...")` based on results
4. **Delete**: `manage_skill(action="delete", name="...")` if no longer needed
5. **List**: `manage_skill(action="list")` to see all installed skills
