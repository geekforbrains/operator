# SKILL.md Frontmatter Reference

Complete field reference per the [agentskills.io](https://agentskills.io/specification) spec.

## Required fields

### `name`

Short identifier for the skill. Must match the parent directory name.

- 1-64 characters
- Lowercase letters, numbers, and hyphens only
- Must not start or end with a hyphen
- Must not contain consecutive hyphens (`--`)
- Pattern: `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`

Valid: `pdf-processing`, `data-analysis`, `code-review`, `my-tool`
Invalid: `PDF-Processing`, `-pdf`, `pdf--processing`, `pdf-`

### `description`

What the skill does and when to use it. This is the primary signal agents use for skill activation.

- 1-1024 characters
- Must be non-empty
- Write in third person ("Generates..." not "I generate...")
- Include both what it does AND specific trigger keywords

## Optional fields

### `license`

License name or reference to a bundled license file.

```yaml
license: MIT
```

```yaml
license: Proprietary. See LICENSE.txt for terms.
```

### `compatibility`

Environment requirements. Only include if the skill has specific needs.

- Max 500 characters

```yaml
compatibility: Requires git, docker, and internet access
```

### `metadata`

Arbitrary key-value mapping for additional metadata.

```yaml
metadata:
  author: my-org
  version: "2.1"
  env:
    - API_KEY
    - DATABASE_URL
```

**`metadata.env`** (Operator convention): List of environment variable names the skill requires. Operator checks these at startup and warns if any are missing. The user sets actual values in their `.env` file configured via `runtime.env_file` in `operator.yaml`.

### `allowed-tools`

Space-delimited list of pre-approved tools. Experimental — support varies by agent.

```yaml
allowed-tools: run_shell read_file write_file
```

## Optional directories

### `scripts/`

Executable code the agent can run. Scripts should be self-contained, handle errors gracefully, and include helpful error messages.

### `references/`

Additional documentation loaded on demand. Keep individual files focused. Use descriptive names (`api-schema.md`, not `ref1.md`).

### `assets/`

Static resources: templates, images, data files, schemas.

## Full example

```yaml
---
name: deploy-staging
description: >-
  Deploys the current branch to the staging environment using Docker Compose.
  Use when asked to deploy, push to staging, or test in a staging environment.
license: MIT
compatibility: Requires docker, docker-compose, and SSH access to staging server
metadata:
  author: platform-team
  version: "1.0"
  env:
    - STAGING_HOST
    - STAGING_SSH_KEY
    - DOCKER_REGISTRY
---
```
