from __future__ import annotations
from dataclasses import dataclass
from ..contracts import HarnessError, MemoryRecall, MemoryRemember, TaskContext, WorkspaceNote, WorkspaceRead
from ..feature_contracts import HarnessRead, IssueCreate, IssueGet, ProjectFiles, ProjectRead

@dataclass
class ContextTools:
    context: object
    feedback: object
    memory: object
    project: object
    recall_memory: object
    task_memory: object
    workspace: object
    workspace_brief: object

    definitions = {
        'task_context': (
        TaskContext,
        "Read bounded workspace, checkpoint, 3 skill cards, 1 selected contract and 3 memories",
    ),
        'workspace_read': (
        WorkspaceRead,
        "Read a workspace brief (no arguments), find note cards (query), or read one note (key)",
    ),
        'project_read': (ProjectRead, "Read purpose-selected project material cards or one body; host fixes purpose"),
        'project_files': (ProjectFiles, "List one selected project directory or read one UTF-8 source; use next_offset to continue; onboarding/improve only"),
        'harness_read': (HarnessRead, "Read installed integration/interfaces public guide text with source identity and next_offset; not robot capability evidence"),
        'issue_create': (IssueCreate, "Save a reasoned local/framework issue with selected evidence; no automatic diagnosis"),
        'issue_get': (IssueGet, "Read at most 3 issue/update states or one linked issue and replies"),
        'workspace_note': (
        WorkspaceNote,
        "Save or update a sourced workspace note by key; archive=true removes it from retrieval",
    ),
        'memory_recall': (MemoryRecall, "Retrieve this runtime's scoped evidence; verified by default"),
        'memory_remember': (MemoryRemember, "Save a candidate lesson backed by a completed operation"),
    }

    native_tools = ('task_context', 'workspace_read', 'workspace_note', 'memory_recall', 'project_read', 'project_files', 'issue_create', 'issue_get', 'harness_read')
    motion_tools = set()
    operation_tools = set()

    async def dispatch(self, name, args, *, task_id=None):
        if name == "task_context":
            return self.context(args)
        if name == "workspace_read":
            if args.query is None and args.key is None:
                return self.workspace_brief(task_id)
            return self.workspace.read(args)
        if name == "project_read":
            return self.project.read(args)
        if name == "project_files":
            return self.project.files(args)
        if name == "harness_read":
            from ..guides import read_guide
            return read_guide(args, self.task_memory.installation_digest)
        if name == "issue_create":
            return self.feedback.create(args)
        if name == "issue_get":
            return self.feedback.get(args, task_id)
        if name == "workspace_note":
            return self.workspace.note(args)
        if name == "memory_recall":
            return self.recall_memory(args, task_id)
        if name == "memory_remember":
            return self.memory.remember(args)
        raise HarnessError("unknown_tool", "Tool not found")
