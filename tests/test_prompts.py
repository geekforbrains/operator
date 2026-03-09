from __future__ import annotations

from operator_ai.config import Config
from operator_ai.prompts import CACHE_BOUNDARY, assemble_system_prompt
from operator_ai.tools.subagent import _build_subagent_prompt


def test_assemble_system_prompt_keeps_dynamic_context_after_cache_boundary(monkeypatch) -> None:
    monkeypatch.setattr("operator_ai.prompts.load_system_prompt", lambda: "# System")
    monkeypatch.setattr(
        "operator_ai.prompts.load_agent_prompt",
        lambda _config, agent_name: f"# Agent\n\n{agent_name}",
    )
    monkeypatch.setattr(
        "operator_ai.prompts.load_skills_prompt",
        lambda _skills_dir, **_kwargs: "",
    )

    prompt = assemble_system_prompt(
        config=Config(
            runtime={"timezone": "America/Vancouver"},
            defaults={"models": ["test/model"]},
            agents={"operator": {}},
        ),
        agent_name="operator",
        context_sections=["# Context\n\nMessage context"],
        available_agents=[],
    )

    stable, dynamic = prompt.split(CACHE_BOUNDARY, 1)
    assert stable == "# System\n\n# Agent\n\noperator"
    assert "# Context\n\nMessage context" in dynamic


def test_subagent_prompt_uses_shared_prompt_contract(monkeypatch) -> None:
    monkeypatch.setattr("operator_ai.prompts.load_system_prompt", lambda: "# System")
    monkeypatch.setattr(
        "operator_ai.prompts.load_agent_prompt",
        lambda _config, agent_name: f"# Agent\n\n{agent_name}",
    )
    monkeypatch.setattr(
        "operator_ai.prompts.load_skills_prompt",
        lambda _skills_dir, **_kwargs: "",
    )

    prompt = _build_subagent_prompt(
        {
            "config": Config(
                runtime={"timezone": "America/Toronto"},
                defaults={"models": ["test/model"]},
                agents={"operator": {}},
            )
        },
        target_agent="operator",
        context="Focus on the timezone-aware interpretation.",
    )

    stable, dynamic = prompt.split(CACHE_BOUNDARY, 1)
    assert stable.startswith("# System\n\n# Agent\n\noperator")
    assert "You are a focused sub-agent." in dynamic
    assert "Focus on the timezone-aware interpretation." in dynamic
