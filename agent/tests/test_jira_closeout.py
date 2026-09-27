"""The ticket always gets a closing comment, and the agent never reads its own
comments back as a user reply. Run: python test_jira_closeout.py"""
import asyncio

import agent as agent_mod
from tools import jira_client

a = agent_mod.ProvisioningAgent()

posted: list[str] = []
transitions: list[str] = []
watermarks: list[str] = []
next_id = [500]


async def fake_add_comment(run_id, key, body):
    posted.append(body)
    next_id[0] += 1
    return str(next_id[0])


async def fake_transition(run_id, key, target=None):
    transitions.append(key)
    return True


jira_client.add_comment = fake_add_comment
jira_client.transition_ticket = fake_transition
a._get_run = lambda run_id: {"goal": "Execute KAN-10: Create an EC2 instance\n\nt3.micro"}
a._set_run_field = lambda run_id, col, val: watermarks.append(f"{col}={val}")
a._add_message = lambda *args, **kwargs: None


async def main():
    # Applied, with a PR.
    await a.close_out_jira("r1", "KAN-10", pr_url="https://github.com/x/y/pull/3", applied=True)
    body = posted[-1]
    assert "Done — Create an EC2 instance" in body, body
    assert "applied to AWS" in body
    assert "pull/3" in body
    assert transitions == ["KAN-10"]

    # No repo connected: still comments, and says so instead of going silent.
    await a.close_out_jira("r2", "KAN-11", applied=True)
    assert "no GitHub repository is connected" in posted[-1], posted[-1]

    # Pushed for review but not applied: must not claim it was deployed.
    await a.close_out_jira("r3", "KAN-12", pr_url="http://p/1", applied=False)
    assert "not applied yet" in posted[-1] and "approve" in posted[-1].lower(), posted[-1]
    assert "applied to AWS" not in posted[-1]

    # Push failed: the reason reaches the ticket.
    await a.close_out_jira("r4", "KAN-13", applied=True, push_error="empty repository")
    assert "empty repository" in posted[-1]

    # Every comment advanced the reply watermark, so the poller can't feed our
    # own comment back to us as an answer.
    assert len(watermarks) == len(posted), f"{watermarks} vs {len(posted)} comments"
    assert all(w.startswith("jira_last_comment_id=") for w in watermarks), watermarks

    print(f"ok: {len(posted)} close-out comments, all watermarked, none mislabelled")


asyncio.run(main())
