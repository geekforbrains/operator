from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import operator_ai.tools  # noqa: F401 — warm up tool imports
from operator_ai.tools import messaging, workspace
from operator_ai.transport.base import Attachment, IncomingMessage, Transport

# --- Attachment dataclass ---


def test_attachment_defaults():
    att = Attachment(
        filename="photo.png",
        content_type="image/png",
        size=1024,
        url="https://slack.com/files/photo.png",
    )
    assert att.platform_id == ""
    assert att.size == 1024


def test_incoming_message_attachments_default():
    msg = IncomingMessage(
        text="hello",
        user_id="U1",
        channel_id="C1",
        message_id="ts1",
        root_message_id="ts1",
        transport_name="test",
    )
    assert msg.attachments == []


def test_incoming_message_with_attachments():
    att = Attachment("f.txt", "text/plain", 100, "http://example.com/f.txt", "F1")
    msg = IncomingMessage(
        text="see file",
        user_id="U1",
        channel_id="C1",
        message_id="ts1",
        root_message_id="ts1",
        transport_name="test",
        attachments=[att],
    )
    assert len(msg.attachments) == 1
    assert msg.attachments[0].filename == "f.txt"


# --- Transport base default methods ---


def test_transport_download_file_raises():
    from operator_ai.transport.cli import CliTransport

    t = CliTransport(agent_name="test")
    att = Attachment("f.txt", "text/plain", 10, "http://example.com/f.txt")
    with pytest.raises(NotImplementedError, match="does not support file downloads"):
        asyncio.run(t.download_file(att))


def test_transport_send_file_raises():
    from operator_ai.transport.cli import CliTransport

    t = CliTransport(agent_name="test")
    with pytest.raises(NotImplementedError, match="does not support file uploads"):
        asyncio.run(t.send_file("C1", b"data", "test.txt"))


# --- Slack attachment extraction ---


def test_slack_extract_attachments():
    from operator_ai.transport.slack import _extract_attachments

    event = {
        "files": [
            {
                "id": "F1",
                "name": "photo.png",
                "mimetype": "image/png",
                "size": 2048,
                "url_private": "https://files.slack.com/F1/photo.png",
            },
            {
                "id": "F2",
                "name": "doc.pdf",
                "mimetype": "application/pdf",
                "size": 50000,
                "url_private": "https://files.slack.com/F2/doc.pdf",
            },
        ]
    }
    attachments = _extract_attachments(event)
    assert len(attachments) == 2
    assert attachments[0].filename == "photo.png"
    assert attachments[0].content_type == "image/png"
    assert attachments[0].platform_id == "F1"
    assert attachments[1].filename == "doc.pdf"


def test_slack_extract_attachments_no_files():
    from operator_ai.transport.slack import _extract_attachments

    assert _extract_attachments({}) == []
    assert _extract_attachments({"files": []}) == []


def test_slack_extract_attachments_skips_no_url():
    from operator_ai.transport.slack import _extract_attachments

    event = {"files": [{"id": "F1", "name": "broken.txt", "mimetype": "text/plain", "size": 10}]}
    assert _extract_attachments(event) == []


# --- process_attachments ---


def test_process_attachments_image_inline(tmp_path: Path):
    from operator_ai.main import process_attachments

    image_bytes = b"\x89PNG\r\n\x1a\nfake-image-data"
    transport = AsyncMock(spec=Transport)
    transport.download_file = AsyncMock(return_value=image_bytes)

    att = Attachment("photo.png", "image/png", len(image_bytes), "http://example.com/photo.png")
    blocks = asyncio.run(process_attachments([att], transport, tmp_path))

    assert len(blocks) == 2
    assert blocks[0]["type"] == "text"
    assert "inbox/photo.png" in blocks[0]["text"]
    assert blocks[1]["type"] == "image_url"
    expected_b64 = base64.b64encode(image_bytes).decode()
    assert blocks[1]["image_url"]["url"] == f"data:image/png;base64,{expected_b64}"
    assert (tmp_path / "inbox" / "photo.png").read_bytes() == image_bytes


def test_process_attachments_non_image_saves_to_disk(tmp_path: Path):
    from operator_ai.main import process_attachments

    pdf_bytes = b"%PDF-1.4 fake pdf content"
    transport = AsyncMock(spec=Transport)
    transport.download_file = AsyncMock(return_value=pdf_bytes)

    att = Attachment("report.pdf", "application/pdf", len(pdf_bytes), "http://example.com/r.pdf")
    blocks = asyncio.run(process_attachments([att], transport, tmp_path))

    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert "report.pdf" in blocks[0]["text"]
    assert (tmp_path / "inbox" / "report.pdf").exists()
    assert (tmp_path / "inbox" / "report.pdf").read_bytes() == pdf_bytes


def test_process_attachments_download_failure(tmp_path: Path):
    from operator_ai.main import process_attachments

    transport = AsyncMock(spec=Transport)
    transport.download_file = AsyncMock(side_effect=RuntimeError("network error"))

    att = Attachment("fail.txt", "text/plain", 100, "http://example.com/fail.txt")
    blocks = asyncio.run(process_attachments([att], transport, tmp_path))

    assert len(blocks) == 1
    assert "failed to download" in blocks[0]["text"]


def test_process_attachments_duplicate_filename(tmp_path: Path):
    from operator_ai.main import process_attachments

    transport = AsyncMock(spec=Transport)
    transport.download_file = AsyncMock(return_value=b"data")

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "file.txt").write_bytes(b"existing")

    att = Attachment("file.txt", "text/plain", 4, "http://example.com/file.txt")
    blocks = asyncio.run(process_attachments([att], transport, tmp_path))

    assert "file_1.txt" in blocks[0]["text"]
    assert (inbox / "file_1.txt").exists()


def test_process_attachments_oversized_skipped(tmp_path: Path):
    from operator_ai.main import MAX_DOWNLOAD_SIZE, process_attachments

    transport = AsyncMock(spec=Transport)
    att = Attachment("huge.bin", "application/octet-stream", MAX_DOWNLOAD_SIZE + 1, "http://x/huge")
    blocks = asyncio.run(process_attachments([att], transport, tmp_path))

    assert len(blocks) == 1
    assert "too large" in blocks[0]["text"]
    transport.download_file.assert_not_called()


def test_process_attachments_sanitizes_filename(tmp_path: Path):
    from operator_ai.main import process_attachments

    transport = AsyncMock(spec=Transport)
    transport.download_file = AsyncMock(return_value=b"data")

    att = Attachment("../../etc/passwd", "text/plain", 4, "http://x/bad")
    blocks = asyncio.run(process_attachments([att], transport, tmp_path))

    assert len(blocks) == 1
    assert "passwd" in blocks[0]["text"]
    # File should be in inbox dir, not escaped
    assert (tmp_path / "inbox" / "passwd").exists()
    assert not (tmp_path / "etc").exists()


# --- send_file tool ---


@dataclass
class FakeTransport:
    sent_files: list[tuple] = field(default_factory=list)
    resolve_result: str | None = "C1"

    async def resolve_channel_id(self, channel: str) -> str | None:  # noqa: ARG002
        return self.resolve_result

    async def send_file(
        self,
        channel_id: str,
        file_data: bytes,
        filename: str,
        thread_id: str | None = None,
    ) -> str:
        self.sent_files.append((channel_id, file_data, filename, thread_id))
        return "msg-123"


def test_send_file_tool(tmp_path: Path):
    workspace.set_workspace(tmp_path)
    (tmp_path / "output.txt").write_text("hello world")
    transport = FakeTransport()
    messaging.configure({"transport": transport, "channel_id": "C1", "thread_id": "t1"})

    result = asyncio.run(messaging.send_file("output.txt"))
    assert result == "msg-123"
    assert len(transport.sent_files) == 1
    assert transport.sent_files[0][0] == "C1"  # default channel
    assert transport.sent_files[0][2] == "output.txt"
    assert transport.sent_files[0][3] == "t1"  # default thread


def test_send_file_tool_explicit_channel(tmp_path: Path):
    workspace.set_workspace(tmp_path)
    (tmp_path / "output.txt").write_text("hello world")
    transport = FakeTransport()
    messaging.configure({"transport": transport, "channel_id": "C1", "thread_id": "t1"})

    result = asyncio.run(messaging.send_file("output.txt", channel="C1", thread_id="t2"))
    assert result == "msg-123"
    assert transport.sent_files[0][3] == "t2"  # explicit thread overrides default


def test_send_file_tool_not_found(tmp_path: Path):
    workspace.set_workspace(tmp_path)
    transport = FakeTransport()
    messaging.configure({"transport": transport, "channel_id": "C1", "thread_id": "t1"})

    result = asyncio.run(messaging.send_file("missing.txt"))
    assert "not found" in result


def test_send_file_tool_outside_workspace_resolves(tmp_path: Path):
    workspace.set_workspace(tmp_path)
    transport = FakeTransport()
    messaging.configure({"transport": transport, "channel_id": "C1", "thread_id": "t1"})

    # Paths outside workspace are allowed; the file just needs to exist
    result = asyncio.run(messaging.send_file("../../nonexistent_file.txt"))
    assert "not found" in result
