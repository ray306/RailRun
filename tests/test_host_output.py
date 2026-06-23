from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.host_output import CodexOutputProvider


def _line(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False) + "\n"


class CodexOutputProviderTests(unittest.TestCase):
    def test_locates_by_thread_id_and_validates_session_meta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = root / "2026" / "06" / "18" / "rollout-thread-123.jsonl"
            expected.parent.mkdir(parents=True)
            expected.write_text(
                _line({"type": "session_meta", "payload": {"id": "thread-123"}}),
                encoding="utf-8",
            )
            newer_wrong = root / "newer-thread-123.jsonl"
            newer_wrong.write_text(
                _line({"type": "session_meta", "payload": {"id": "other-thread"}}),
                encoding="utf-8",
            )

            provider = CodexOutputProvider(
                env={"CODEX_THREAD_ID": "thread-123"},
                sessions_root=root,
            )
            transcript = provider.locate_current_transcript()

            self.assertIsNotNone(transcript)
            self.assertEqual(transcript.path, expected.resolve())
            self.assertEqual(transcript.session_id, "thread-123")

    def test_reads_only_new_event_agent_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-thread-123.jsonl"
            path.write_text(
                _line({"type": "session_meta", "payload": {"id": "thread-123"}}),
                encoding="utf-8",
            )
            provider = CodexOutputProvider(
                env={"CODEX_THREAD_ID": "thread-123"},
                sessions_root=Path(tmp),
            )
            transcript = provider.locate_current_transcript()
            self.assertIsNotNone(transcript)

            with path.open("a", encoding="utf-8") as handle:
                handle.write(_line({
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "正式输出一", "phase": "commentary"},
                }))
                handle.write(_line({
                    "type": "response_item",
                    "payload": {"type": "message", "role": "assistant"},
                }))
                handle.write(_line({
                    "type": "event_msg",
                    "payload": {"type": "token_count"},
                }))
                handle.write(_line({
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "正式输出二", "phase": "final_answer"},
                }))

            result = provider.read_new_formal_messages(transcript)
            self.assertEqual(result.messages, ["正式输出一", "正式输出二"])
            self.assertGreater(result.offset, transcript.offset)

    def test_rejects_transcript_if_session_meta_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-thread-123.jsonl"
            path.write_text(
                _line({"type": "session_meta", "payload": {"id": "thread-123"}}),
                encoding="utf-8",
            )
            provider = CodexOutputProvider(
                env={"CODEX_THREAD_ID": "thread-123"},
                sessions_root=Path(tmp),
            )
            transcript = provider.locate_current_transcript()
            self.assertIsNotNone(transcript)
            path.write_text(
                _line({"type": "session_meta", "payload": {"id": "other-thread"}}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "会话校验失败"):
                provider.read_new_formal_messages(transcript)


if __name__ == "__main__":
    unittest.main()
