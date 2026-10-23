from __future__ import annotations
from dataclasses import dataclass
from ..contracts import AssistanceClaim, AssistanceReturn, HarnessError, TaskIdentity


@dataclass
class SessionTools:
    agent_state: object

    definitions = {
        'agent_session_get': (
        TaskIdentity,
        "Read latest client session and original task/assistance state",
    ),
        'assistance_get': (
        TaskIdentity,
        "Read deterministic role, evidence and allowed actions for a task",
    ),
        'assistance_claim': (
        AssistanceClaim,
        "Host-only operator: revoke old model decisions and claim a task",
    ),
        'assistance_return': (
        AssistanceReturn,
        "Host-only operator: explicitly return control or hand off unknown effects",
    ),
    }

    native_tools = ('assistance_get',)
    motion_tools = set()
    operation_tools = set()

    async def dispatch(self, name, args, *, task_id=None):
        if name == "agent_session_get":
            return self.agent_state().status(args.task_id)
        if name == "assistance_get":
            return self.agent_state().request(args.task_id)
        if name == "assistance_claim":
            return await self.agent_state().claim(args)
        if name == "assistance_return":
            return self.agent_state().give_back(args)
        raise HarnessError("unknown_tool", "Tool not found")
