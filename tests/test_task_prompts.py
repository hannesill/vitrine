"""Tests for the study task prompts copied from the board's action palette."""

from pathlib import Path

import pytest

from vitrine.task_prompts import SKILLS_DIR, TASK_SKILLS, build_task_prompt


@pytest.mark.parametrize("task", sorted(TASK_SKILLS))
def test_prompt_heads_skill_with_study_context(task):
    prompt = build_task_prompt(task, "sepsis-v1", Path("/data/sepsis/output"))

    header, body = prompt.split("\n\n---\n\n", 1)
    assert "`sepsis-v1`" in header
    assert "**Output directory:** `/data/sepsis/output`" in header
    # The skill body follows without its YAML frontmatter
    assert body.startswith("# ")
    assert "\nname: " not in prompt.split("# ", 1)[0]


def test_prompt_without_output_dir_omits_it():
    prompt = build_task_prompt("report", "s1", None)

    assert "Output directory" not in prompt
    assert "**Study:** `s1`" in prompt


def test_unknown_task_raises():
    with pytest.raises(ValueError, match="Unknown task"):
        build_task_prompt("deploy", "s1", None)


@pytest.mark.parametrize("task, skill", sorted(TASK_SKILLS.items()))
def test_each_task_ships_a_loadable_agent_skill(task, skill):
    text = (SKILLS_DIR / skill / "SKILL.md").read_text()
    frontmatter = text.split("---\n")[1]

    assert f"name: {skill}\n" in frontmatter
    assert "description: " in frontmatter


@pytest.mark.parametrize("skill", sorted(TASK_SKILLS.values()))
def test_skills_do_not_assume_the_removed_agent_runner(skill):
    text = (SKILLS_DIR / skill / "SKILL.md").read_text().lower()

    for phrase in ("dispatch", "additional_prompt", "workspace", "agent card"):
        assert phrase not in text
