from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class SessionState:
    session: str
    procedure_path: str
    dag_path: str
    status: str = "running"
    # node_id is initialized by runtime from DAG/CFG entry.
    cursor: dict[str, Any] = field(default_factory=lambda: {"step_index": 0, "node_id": None})
    waiting_for_branch: bool = False
    waiting_branch_node: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    history: list[dict[str, Any]] = field(default_factory=list)
    vars: dict[str, Any] = field(default_factory=dict)
    for_cursors: dict[str, int] = field(default_factory=dict)
    output_persistence_enabled: bool = True
    host_output_capture: dict[str, Any] | None = None
    language: str = "中文"
    language_message_emitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "procedure_path": self.procedure_path,
            "dag_path": self.dag_path,
            "status": self.status,
            "cursor": self.cursor,
            "waiting_for_branch": self.waiting_for_branch,
            "waiting_branch_node": self.waiting_branch_node,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "history": self.history,
            "vars": self.vars,
            "for_cursors": self.for_cursors,
            "output_persistence_enabled": self.output_persistence_enabled,
            "host_output_capture": self.host_output_capture,
            "language": self.language,
            "language_message_emitted": self.language_message_emitted,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionState":
        return cls(
            session=data["session"],
            procedure_path=data["procedure_path"],
            dag_path=data["dag_path"],
            status=data.get("status", "running"),
            cursor=data.get("cursor", {"step_index": 0, "node_id": None}),
            waiting_for_branch=data.get("waiting_for_branch", False),
            waiting_branch_node=data.get("waiting_branch_node"),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
            history=data.get("history", []),
            vars=data.get("vars", {}),
            for_cursors=data.get("for_cursors", {}),
            output_persistence_enabled=data.get("output_persistence_enabled", True),
            host_output_capture=data.get("host_output_capture"),
            language=data.get("language", "中文"),
            language_message_emitted=data.get("language_message_emitted", False),
        )
