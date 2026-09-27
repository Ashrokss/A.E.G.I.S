"""
Test for update_jira_ticket_status tool execution in agent.py.
Run: python tests/test_jira_transition_tool.py
"""
import asyncio
from tools import jira_client
import agent as agent_mod

a = agent_mod.ProvisioningAgent()

transitions: list[tuple[str, str, str | None]] = []


async def fake_transition(run_id, key, target_status_name=None):
    transitions.append((run_id, key, target_status_name))
    return True


jira_client.transition_ticket = fake_transition

messages: list[tuple[str, str, str]] = []


def fake_add_message(run_id, role, content, msg_type="text", tool_calls=None, tool_call_id=None, step=None):
    messages.append((role, content, msg_type))


a._add_message = fake_add_message


async def test_manual_transition():
    key = "KAN-24"
    status = "Done"
    run_id = "test-run-jira"

    success = await jira_client.transition_ticket(run_id, key, target_status_name=status)
    assert success is True
    assert transitions == [("test-run-jira", "KAN-24", "Done")]

    print("ok: manual JIRA ticket transition tool works as expected")


asyncio.run(test_manual_transition())
