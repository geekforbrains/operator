# Principles Audit

This file tracks the ongoing section-by-section review of `PRINCIPLES.md`
against the current implementation. Review one section at a time, record the
status, and note only the concrete changes made to bring code and principles
back into alignment.

## Latest Changes

- `Design Goals`: complete
- Refactored transport config into a generic `type` plus validated `env` and
  `settings` sections, so config loading and runtime transport creation keep
  secrets, non-secret transport behavior, and transport type distinct.
- Added regression coverage for inline transport config, explicit
  transport env/settings config, starter-config generation, and runtime
  transport creation.
- Updated bundled prompt/skill guidance and runtime status labels to use the
  current state tool names instead of removed KV/read-write terminology.
- `Core Use Case`: complete
- Removed the sandbox concept from runtime config, subagent handoff plumbing,
  and related docs/tests.
- Clarified in `PRINCIPLES.md` that agents should have the smallest useful tool
  and skill surface, with shell access granted sparingly.
- Updated the core loop wording so agents return results to the user or
  destination when needed, which better matches scheduled jobs.
