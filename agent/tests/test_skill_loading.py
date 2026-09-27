"""Both agents load their own skill, and a missing skill fails loudly instead of
running the agent with no instructions. Run: python -m tests.test_skill_loading"""
from utils.skills import load_skill

# The provisioning prompt resolves through the shared loader.
import agent as agent_mod

tf = load_skill("terraform-provisioning")
assert tf, "terraform-provisioning SKILL.md is empty"
assert tf == agent_mod._SKILL_PROMPT, "agent.py no longer uses the shared loader"
# Doc discovery and the IAM boundary are enforced in code; the prompt must say so too.
assert "search_terraform_docs" in tf, "the skill must tell the agent how to find docs"
assert "permissions_boundary" in tf, "the skill must require the AEGIS permissions boundary"

kb = load_skill("kb-chat")
assert kb, "kb-chat SKILL.md is empty"

try:
    load_skill("no-such-skill")
except (FileNotFoundError, OSError):
    pass
else:
    raise AssertionError("an unknown skill must raise, not return empty")

print(f"ok: 2 skills load ({len(tf)} / {len(kb)} chars), unknown skill raises")
