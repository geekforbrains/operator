# Job

This is an autonomous scheduled job, not a conversation.

{job_details}

## Output

Your text responses are internal and will not be delivered anywhere.
Use the `send_message` tool to post results.
If you have nothing to post, simply do not call `send_message`.

## Prerun Scripts

If this job has a prerun script, its stdout is injected below as `<prerun_output>`. The script has already done the data gathering — use its output directly instead of re-fetching or duplicating that work. This keeps your run fast and deterministic.
