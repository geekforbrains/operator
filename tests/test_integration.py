#!/usr/bin/env python3
"""Test bot event parsing without Telegram."""

import asyncio

from operator_agent.core import Runtime


def _make_runtime():
    """Create a minimal Runtime for testing."""
    config = {
        "working_dir": ".",
        "providers": {
            "claude": {"path": "claude", "models": ["opus", "sonnet", "haiku"]},
            "codex": {"path": "codex", "models": ["gpt-5.3-codex"]},
            "gemini": {"path": "gemini", "models": ["gemini-2.5-pro", "gemini-2.5-flash"]},
        },
    }
    rt = Runtime(config)
    rt.init_config_dir()
    rt.load_state()
    return rt


async def test_claude():
    """Test Claude CLI integration."""
    print("Testing Claude...")
    rt = _make_runtime()
    provider = rt.make_provider("claude")
    response_text = ""
    chat_id = 999

    async for event in rt.run_provider(
        provider, "Say 'Claude works' and nothing else", chat_id, "sonnet"
    ):
        if event.kind == "status":
            print(f"  Status: {event.text}")
        elif event.kind == "response":
            response_text = event.text

    print(f"  Result: {response_text.strip()}")
    if "Claude works" in response_text:
        print("  Claude test passed!")
    else:
        print(f"  Claude test failed: {response_text}")


async def test_codex():
    """Test Codex CLI integration."""
    print("\nTesting Codex...")
    rt = _make_runtime()
    provider = rt.make_provider("codex")
    response_text = ""
    chat_id = 999

    async for event in rt.run_provider(
        provider, "Say 'Codex works' and nothing else", chat_id, "gpt-5.3-codex"
    ):
        if event.kind == "status":
            print(f"  Status: {event.text}")
        elif event.kind == "response":
            response_text = event.text

    print(f"  Result: {response_text.strip()}")
    if "Codex works" in response_text:
        print("  Codex test passed!")
    else:
        print(f"  Codex test failed: {response_text}")


async def test_gemini():
    """Test Gemini CLI integration."""
    print("\nTesting Gemini...")
    rt = _make_runtime()
    provider = rt.make_provider("gemini")
    response_text = ""
    chat_id = 999

    async for event in rt.run_provider(
        provider, "Say 'Gemini works' and nothing else", chat_id, "gemini-2.5-pro"
    ):
        if event.kind == "status":
            print(f"  Status: {event.text}")
        elif event.kind == "response":
            response_text = event.text

    print(f"  Result: {response_text.strip()}")
    if "Gemini works" in response_text:
        print("  Gemini test passed!")
    else:
        print(f"  Gemini test failed: {response_text}")


async def main():
    print("=== Operator Agent Integration Tests ===\n")
    try:
        await test_claude()
        await test_codex()
        await test_gemini()
        print("\nAll integration tests completed.")
    except Exception as e:
        print(f"\n  Error during tests: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
