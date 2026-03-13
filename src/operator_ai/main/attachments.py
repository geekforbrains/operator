from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path

from operator_ai.transport.base import Attachment, Transport

logger = logging.getLogger("operator")

_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
MAX_INLINE_SIZE = 5 * 1024 * 1024  # 5 MB — larger images saved to disk instead
MAX_DOWNLOAD_SIZE = 50 * 1024 * 1024  # 50 MB — skip oversized files


async def process_attachments(
    attachments: list[Attachment],
    transport: Transport,
    workspace: Path,
) -> list[dict]:
    """Download attachments and return multimodal content blocks.

    All attachments are saved to workspace/inbox/ so they remain available as
    workspace artifacts. Small images are also inlined as base64 image_url
    blocks for direct visual inspection by the model.
    """
    blocks: list[dict] = []
    inbox_dir = workspace / "inbox"

    for att in attachments:
        if att.size > MAX_DOWNLOAD_SIZE:
            blocks.append(
                {"type": "text", "text": f"[skipped: {att.filename} too large ({att.size} bytes)]"}
            )
            continue

        try:
            data = await transport.download_file(att)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Failed to download attachment %s", att.filename, exc_info=True)
            blocks.append({"type": "text", "text": f"[failed to download: {att.filename}]"})
            continue

        inbox_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(att.filename).name or "unnamed"
        dest = inbox_dir / safe_name
        # Avoid overwriting — append suffix if needed
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            counter = 1
            while dest.exists():
                dest = inbox_dir / f"{stem}_{counter}{suffix}"
                counter += 1
        dest.write_bytes(data)
        blocks.append(
            {
                "type": "text",
                "text": f"[file saved: inbox/{dest.name} ({att.content_type}, {len(data)} bytes)]",
            }
        )

        if att.content_type in _IMAGE_TYPES and len(data) <= MAX_INLINE_SIZE:
            b64 = base64.b64encode(data).decode()
            blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{att.content_type};base64,{b64}"},
                }
            )

    return blocks
