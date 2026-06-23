from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HostTranscript:
    host: str
    session_id: str
    path: Path
    offset: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "session_id": self.session_id,
            "path": str(self.path),
            "offset": self.offset,
        }


@dataclass(frozen=True)
class TranscriptRead:
    messages: list[str]
    offset: int


class HostOutputProvider(ABC):
    name: str

    @abstractmethod
    def locate_current_transcript(self) -> HostTranscript | None:
        """Locate and validate the transcript for the current host session."""

    @abstractmethod
    def read_new_formal_messages(self, transcript: HostTranscript) -> TranscriptRead:
        """Read formal host messages appended after transcript.offset."""


class CodexOutputProvider(HostOutputProvider):
    name = "codex"

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        sessions_root: Path | None = None,
    ) -> None:
        self.env = os.environ if env is None else env
        codex_home = Path(self.env.get("CODEX_HOME", Path.home() / ".codex"))
        self.sessions_root = (sessions_root or codex_home / "sessions").resolve()

    @staticmethod
    def _session_meta_id(path: Path) -> str | None:
        try:
            with path.open("r", encoding="utf-8") as handle:
                first_line = handle.readline()
            record = json.loads(first_line)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if record.get("type") != "session_meta":
            return None
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return None
        session_id = payload.get("id")
        return session_id if isinstance(session_id, str) else None

    def locate_current_transcript(self) -> HostTranscript | None:
        session_id = self.env.get("CODEX_THREAD_ID", "").strip()
        if not session_id or not self.sessions_root.is_dir():
            return None

        # Match by the host session ID, never by modification time. The
        # session_meta check below is the authoritative ownership validation.
        candidates = sorted(self.sessions_root.rglob(f"*{session_id}*.jsonl"))
        validated = [
            path.resolve()
            for path in candidates
            if self._session_meta_id(path) == session_id
        ]
        if len(validated) != 1:
            return None

        path = validated[0]
        try:
            offset = path.stat().st_size
        except OSError:
            return None
        return HostTranscript(self.name, session_id, path, offset)

    def read_new_formal_messages(self, transcript: HostTranscript) -> TranscriptRead:
        path = transcript.path.resolve()
        if self._session_meta_id(path) != transcript.session_id:
            raise ValueError("Codex transcript 会话校验失败。")

        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ValueError(f"无法读取 Codex transcript: {exc}") from exc
        if transcript.offset < 0 or transcript.offset > size:
            raise ValueError("Codex transcript 字节游标无效。")

        messages: list[str] = []
        committed_offset = transcript.offset
        try:
            with path.open("rb") as handle:
                handle.seek(transcript.offset)
                while True:
                    line = handle.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        break
                    committed_offset = handle.tell()
                    try:
                        record = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if record.get("type") != "event_msg":
                        continue
                    payload = record.get("payload")
                    if not isinstance(payload, dict) or payload.get("type") != "agent_message":
                        continue
                    message = payload.get("message")
                    if isinstance(message, str) and message.strip():
                        messages.append(message)
        except OSError as exc:
            raise ValueError(f"无法增量读取 Codex transcript: {exc}") from exc

        return TranscriptRead(messages=messages, offset=committed_offset)


def create_host_output_provider(host: str | None) -> HostOutputProvider | None:
    normalized = (host or "").strip().lower()
    if normalized in {"", "none"}:
        return None
    if normalized in {"auto", "codex"}:
        if normalized == "codex" or os.environ.get("CODEX_THREAD_ID"):
            return CodexOutputProvider()
        return None
    raise ValueError(f"不支持的宿主输出采集器: {host}")


def transcript_from_dict(data: dict[str, Any]) -> HostTranscript:
    return HostTranscript(
        host=str(data["host"]),
        session_id=str(data["session_id"]),
        path=Path(str(data["path"])),
        offset=int(data.get("offset", 0)),
    )
