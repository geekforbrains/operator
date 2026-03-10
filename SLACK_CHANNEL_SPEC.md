# Slack Channel Cache Refactor

Replaces the current always-running Slack channel refresh loop with a
per-bot channel cache that warms once on startup and refreshes lazily on
demand. Archived channels are ignored by default. Slack channel behavior
is configurable per transport/bot in `operator.yaml`.

## Goals

- Keep channel visibility scoped to the bot token actually used by the
  agent. No cross-bot channel sharing.
- Run one initial channel warmup on startup for each configured Slack
  bot, then refresh only when needed.
- Ignore archived channels by default, with per-bot opt-in support.
- Make prompt injection of channels configurable per bot.
- Keep the current agent/transport model. No top-level `transports:`
  refactor in this change.

## Non-goals

- No shared channel directory across different Slack bot tokens
- No change to Slack routing or multi-agent entrypoint behavior
- No background periodic channel refresh loop
- No attempt to infer one bot's channel visibility from another bot

## Current state (what we're replacing)

Each `SlackTransport` instance owns its own in-memory channel caches:

- `_channels`
- `_channel_ids`
- `_channel_info`
- `_refresh_task`

On startup, `SlackTransport.start()` creates an `AsyncApp`, starts a
Socket Mode handler, and launches `_refresh_cache_loop()`. That loop
runs forever and calls `_fetch_all_channels()` every 15 minutes.

Current issues:

- Every Slack bot refreshes channels on a timer even if it never uses
  `list_channels`, `read_channel`, or `send_message` to another channel.
- Archived channels can appear in the cached list and then leak into
  `list_channels`, prompt injection, and name-based resolution.
- Slack behavior is not configurable per transport in `operator.yaml`.
- `get_prompt_extra()` is synchronous, so prompt generation cannot
  trigger an async refresh and must rely on whatever is already cached.

## New design

### Transport-scoped cache, per bot

Keep the channel cache inside `SlackTransport`. In this repo, Slack bot
tokens are always unique, so a shared cross-agent registry is not
useful right now.

The cache remains per transport/bot, but its lifecycle changes:

- warm once on startup
- refresh lazily after TTL expiry
- refresh lazily on channel lookup miss
- no recurring refresh loop

### Config changes

Add optional Slack channel settings to `TransportConfig`:

```python
class TransportConfig(StrictConfigModel):
    type: str
    bot_token_env: str | None = None
    app_token_env: str | None = None
    include_archived_channels: bool = False
    inject_channels_into_prompt: bool = False
    channel_cache_ttl_seconds: int = Field(default=900, gt=0)
    warm_channels_on_startup: bool = True
```

Config example:

```yaml
agents:
  operator:
    transport:
      type: slack
      bot_token_env: SLACK_BOT_TOKEN
      app_token_env: SLACK_APP_TOKEN
      include_archived_channels: false
      inject_channels_into_prompt: false
      channel_cache_ttl_seconds: 900
      warm_channels_on_startup: true
```

These settings are per bot. Since bot tokens are unique, there is no
need to reconcile multiple agents sharing one token in this change.

### SlackTransport lifecycle

Replace the current refresh loop machinery with:

```python
self._channel_cache_refreshed_at: float | None = None
self._channel_cache_lock = asyncio.Lock()
```

Remove:

```python
self._refresh_task: asyncio.Task | None = None
```

Startup behavior:

1. Create `AsyncApp`
2. Register Slack event handlers
3. Create Socket Mode handler
4. If `warm_channels_on_startup` is true, synchronously call
   `_refresh_channel_cache(force=True)`
5. Start Socket Mode handler

Shutdown behavior:

- do not manage a recurring refresh loop

Warmup failures are logged but are not fatal. Lazy refresh paths can
retry later.

### Cache refresh API

Replace `_refresh_cache_loop()` with:

```python
async def _refresh_channel_cache(self, *, force: bool = False) -> None: ...
async def _ensure_channel_cache_fresh(self) -> None: ...
def _channel_cache_is_stale(self) -> bool: ...
```

Behavior:

- If the cache has never been loaded, refresh immediately
- If `channel_cache_ttl_seconds` has elapsed since the last refresh,
  refresh on next use
- Use `asyncio.Lock` so concurrent tool calls do not stampede Slack
- If a cache miss occurs during `resolve_channel_id()`, force one extra
  refresh and retry once before returning `None`

### Channel fetch semantics

`_fetch_all_channels()` remains the source of truth, but its behavior
changes:

- Use `exclude_archived=not self._include_archived_channels` in
  `conversations.list`
- If `include_archived_channels` is false, also skip any channel payload
  where `is_archived` is true as a defensive local filter
- Continue paginating until `next_cursor` is empty
- Populate caches atomically only after a successful full refresh
- Update `_channel_cache_refreshed_at` only on success

The cache contents remain:

- `channel_id -> #name`
- `name -> channel_id`
- `channel_id -> topic/purpose snippet`

No new persistent storage is needed.

### Tool behavior

#### `list_channels`

Before returning data:

```python
await self._ensure_channel_cache_fresh()
```

If refresh fails and the cache is still empty, return a useful error:

```text
[error: failed to load Slack channels]
```

Otherwise return the last known cached snapshot.

Optional improvement in this refactor:

```python
async def list_channels(query: str = "") -> str:
```

If `query` is provided, filter the formatted channel list by name,
topic, or purpose substring match before rendering. This keeps outputs
small without requiring prompt injection.

#### `resolve_channel_id`

Behavior:

- Raw Slack IDs (`C...`, `G...`, `D...`) still return immediately
- Name-based lookups call `_ensure_channel_cache_fresh()`
- On miss, force one refresh and retry

This keeps direct ID usage unchanged while improving stale-name
recovery.

#### `read_channel` and `read_thread`

No direct refresh logic needed. They already flow through
`resolve_channel_id()`.

### Prompt behavior

Keep `Transport.get_prompt_extra()` synchronous.

New behavior:

- Always include the basic messaging instructions
- If `inject_channels_into_prompt` is false, stop there
- If `inject_channels_into_prompt` is true and the cache has entries,
  include the cached channel list
- If `inject_channels_into_prompt` is true but the cache is empty,
  include a short note telling the model to call `list_channels`

Example fallback prompt text:

```text
Use `list_channels` to inspect Slack destinations if you need to send or
read from another channel.
```

This avoids turning prompt assembly into an async operation.

### Startup versus lazy behavior

This refactor intentionally does both:

- one startup warmup to make channel names available quickly
- lazy refresh after that, only when a channel-aware operation happens

This gives predictable first-use behavior without keeping a periodic
refresh loop alive forever.

## Files changed

### Modified

- `src/operator_ai/config.py`
  - add Slack channel settings to `TransportConfig`
- `src/operator_ai/main.py`
  - pass new Slack config values into `SlackTransport`
- `src/operator_ai/transport/slack.py`
  - remove the periodic refresh loop
  - warm the channel cache before Socket Mode blocks
  - add lazy TTL refresh helpers
  - add archived-channel filtering
  - gate prompt injection behind config
  - optionally add `query` filtering to `list_channels`
- `README.md`
  - document the new Slack transport settings

### Unchanged

- `src/operator_ai/tools/messaging.py`
  - still relies on `transport.resolve_channel_id()`
- `src/operator_ai/jobs.py`
  - still uses `transport.get_prompt_extra()` if a transport exists
- transport ownership model
  - Slack transport remains configured under each agent

## Testing

### Unit tests for `tests/test_transport.py`

1. **Startup warmup**
   - when `warm_channels_on_startup` is true, startup schedules a
     one-shot warm task
   - no recurring refresh loop remains

2. **Lazy load on first use**
   - `list_channels()` refreshes when the cache is empty
   - `resolve_channel_id("#general")` refreshes when the cache is empty

3. **TTL refresh**
   - a fresh cache does not re-fetch
   - a stale cache re-fetches on next use

4. **Miss-driven retry**
   - name lookup miss forces one additional refresh and retries once

5. **Archived filtering**
   - with default config, archived channels are omitted from caches and
     output
   - with `include_archived_channels=true`, archived channels are kept

6. **Prompt injection**
   - default config does not inject channel lists into the prompt
   - injected prompt uses cached data only
   - empty cache with injection enabled shows the fallback guidance

7. **Failure handling**
   - failed warmup logs and leaves the transport usable
   - failed lazy refresh returns an error when no cached snapshot exists
   - failed lazy refresh falls back to the last cached snapshot when one
     exists

8. **Optional query filtering**
   - `list_channels(query="deploy")` filters by channel name or snippet

## Rollout notes

- Archived channels are off by default. This is the new default behavior.
- Prompt injection of channel lists is off by default to avoid prompt
  bloat and reliance on sync cache state.
- Existing configs without the new fields continue to work via defaults.

## Summary

This change keeps the simple per-agent Slack transport model, matches
the actual deployment pattern of one unique bot token per Slack bot, and
removes the wasteful periodic channel scan. Startup warmup keeps the
first interaction fast, lazy TTL refresh keeps the cache fresh when it
actually matters, and archived channels stop leaking into agent-visible
channel discovery by default.
