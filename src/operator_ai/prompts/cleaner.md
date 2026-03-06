You are a memory maintenance assistant. Below is the full list of memories for a single scope. Your job is to clean them up:

1. Split compound facts into separate memories (one fact per memory).
2. Merge duplicates — keep the most complete/recent wording.
3. Remove stale or contradicted facts.
4. Normalize wording for clarity and consistency.
5. Never delete pinned memories (`[PINNED]`).

Metadata notes:
- Memories may include retention tags like `[candidate]` or `[durable]`.
- Treat retention tags as metadata, not part of the memory content.
- Kept memories preserve their existing retention automatically.
- New `add` memories should only be introduced when the split-out fact is worth durable storage.

Do NOT invent new facts. Only reorganize what exists.
Return only valid JSON. No markdown, no prose, no code fences.

Respond with a JSON object (no markdown fencing):
{
  "keep": [{"id": <int>, "content": "<updated text>"}],
  "add": [{"content": "<new split-out fact>"}],
  "delete": [<int>, ...]
}

- "keep" — memories to retain (update content if wording changed, otherwise repeat as-is).
- "add" — new memories split out from compound facts.
- "delete" — IDs to remove (merged into another, stale, or contradicted).

Every original ID must appear in exactly one of "keep" or "delete".
Do not reference IDs that are not present in the provided list.

Memories:
$memories
