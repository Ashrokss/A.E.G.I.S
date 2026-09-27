"""
Skill prompt loading.

A skill is a plain Markdown rule set under skills/<name>/SKILL.md that becomes
an agent's system message. There is deliberately no discovery loop: each agent
names the one skill it runs on, so a missing or renamed skill fails loudly at
import time rather than silently degrading the agent to no instructions.
"""
import os

_SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "skills")


def load_skill(name: str) -> str:
    """The named skill's SKILL.md. Raises if it isn't there — see module docstring."""
    path = os.path.join(_SKILLS_DIR, name, "SKILL.md")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
