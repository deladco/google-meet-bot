import argparse
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = Path(
    os.environ.get("MEETING_DB_PATH", str(PROJECT_DIR / "meeting_history.sqlite3"))
)
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "did", "do",
    "for", "from", "how", "i", "in", "is", "it", "of", "on", "or",
    "that", "the", "their", "them", "there", "these", "this", "to",
    "we", "what", "when", "where", "which", "who", "with", "you",
}


class MeetingHistory:
    def __init__(self, db_path=DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        if not self.db_path.is_absolute():
            self.db_path = PROJECT_DIR / self.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS meetings (
                    meeting_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    transcript_path TEXT NOT NULL,
                    notes_path TEXT,
                    minutes_path TEXT,
                    minutes_json TEXT,
                    status TEXT NOT NULL,
                    error TEXT
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(meetings)")
            }
            if "minutes_path" not in columns:
                connection.execute("ALTER TABLE meetings ADD COLUMN minutes_path TEXT")
            if "minutes_json" not in columns:
                connection.execute("ALTER TABLE meetings ADD COLUMN minutes_json TEXT")
            connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS meeting_search
                USING fts5(meeting_id UNINDEXED, kind UNINDEXED, content)
                """
            )

    def create_meeting(
        self,
        meeting_id,
        timestamp,
        transcript_path,
        notes_path=None,
        status="starting",
        minutes_path=None,
    ):
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO meetings (
                    meeting_id, timestamp, transcript_path, notes_path,
                    minutes_path, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(meeting_id) DO UPDATE SET
                    timestamp = excluded.timestamp,
                    transcript_path = excluded.transcript_path,
                    notes_path = excluded.notes_path,
                    minutes_path = excluded.minutes_path,
                    status = excluded.status,
                    error = NULL
                """,
                (
                    str(meeting_id),
                    str(timestamp),
                    str(Path(transcript_path).resolve()),
                    str(Path(notes_path).resolve()) if notes_path else None,
                    str(Path(minutes_path).resolve()) if minutes_path else None,
                    str(status),
                ),
            )

    def update_status(
        self,
        meeting_id,
        status,
        error=None,
        notes_path=None,
        minutes_path=None,
        minutes_json=None,
    ):
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE meetings
                SET status = ?,
                    error = CASE WHEN ? IS NULL THEN error ELSE ? END,
                    notes_path = COALESCE(?, notes_path),
                    minutes_path = COALESCE(?, minutes_path),
                    minutes_json = COALESCE(?, minutes_json)
                WHERE meeting_id = ?
                """,
                (
                    str(status),
                    error,
                    error,
                    str(Path(notes_path).resolve()) if notes_path else None,
                    str(Path(minutes_path).resolve()) if minutes_path else None,
                    minutes_json,
                    str(meeting_id),
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Unknown meeting ID: {meeting_id}")

    def index_file(self, meeting_id, kind, path):
        path = Path(path)
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            return

        with self._connection() as connection:
            meeting = connection.execute(
                "SELECT 1 FROM meetings WHERE meeting_id = ?",
                (str(meeting_id),),
            ).fetchone()
            if meeting is None:
                raise KeyError(f"Unknown meeting ID: {meeting_id}")

            connection.execute(
                "DELETE FROM meeting_search WHERE meeting_id = ? AND kind = ?",
                (str(meeting_id), str(kind)),
            )
            connection.execute(
                "INSERT INTO meeting_search (meeting_id, kind, content) "
                "VALUES (?, ?, ?)",
                (str(meeting_id), str(kind), content),
            )

    @staticmethod
    def _fts_query(query):
        terms = re.findall(r"[\w]+", query, flags=re.UNICODE)
        if not terms:
            raise ValueError("Enter one or more search terms.")
        useful_terms = [term for term in terms if term.casefold() not in STOP_WORDS]
        if not useful_terms:
            useful_terms = terms
        return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in useful_terms)

    def search(self, query, limit=10):
        match = self._fts_query(query)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    meetings.meeting_id,
                    meetings.timestamp,
                    meetings.transcript_path,
                    meetings.notes_path,
                    meetings.status,
                    meeting_search.kind,
                    meeting_search.content,
                    snippet(meeting_search, 2, '[', ']', ' … ', 24) AS excerpt
                FROM meeting_search
                JOIN meetings USING (meeting_id)
                WHERE meeting_search MATCH ?
                ORDER BY bm25(meeting_search)
                LIMIT ?
                """,
                (match, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_meetings(self, limit=20):
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT meeting_id, timestamp, transcript_path, notes_path,
                    minutes_path, minutes_json, status, error
                FROM meetings
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def answer(self, question, limit=5, model="llama3.2:3b"):
        matches = self.search(question, limit=limit)
        if not matches:
            return "No matching meeting history found."

        sources = []
        for match in matches:
            sources.append(
                f"Meeting ID: {match['meeting_id']}\n"
                f"Meeting timestamp: {match['timestamp']}\n"
                f"Source: {match['kind']}\n"
                f"Content:\n{match['content'][:6000]}"
            )

        from ollama import Client

        client = Client(
            host=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
        )
        response = client.chat(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Answer the question using only the supplied meeting-history "
                        "sources. Do not invent decisions, owners, dates, or tasks. "
                        "If the sources do not contain the answer, say so. Cite each "
                        "claim with its Meeting ID and timestamp."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Question: {question}\n\n"
                        "Meeting history sources:\n\n" + "\n\n---\n\n".join(sources)
                    ),
                },
            ],
        )
        return response.message.content


def main():
    parser = argparse.ArgumentParser(description="Search local meeting history.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    commands = parser.add_subparsers(dest="command", required=True)

    list_command = commands.add_parser("list", help="List recorded meetings")
    list_command.add_argument("--limit", type=int, default=20)

    search_command = commands.add_parser("search", help="Search transcripts and notes")
    search_command.add_argument("query")
    search_command.add_argument("--limit", type=int, default=10)

    ask_command = commands.add_parser("ask", help="Ask a question over matching meetings")
    ask_command.add_argument("question")
    ask_command.add_argument("--limit", type=int, default=5)

    args = parser.parse_args()
    history = MeetingHistory(args.db)

    if args.command == "list":
        for meeting in history.list_meetings(args.limit):
            print(
                f"{meeting['timestamp']} | {meeting['meeting_id']} | "
                f"{meeting['status']}\n"
                f"  Transcript: {meeting['transcript_path']}\n"
                f"  Notes: {meeting['notes_path'] or 'Not available'}"
            )
            if meeting["error"]:
                print(f"  Error: {meeting['error']}")
        return

    if args.command == "search":
        results = history.search(args.query, args.limit)
        for result in results:
            print(
                f"{result['timestamp']} | {result['meeting_id']} | "
                f"{result['kind']} | {result['status']}\n"
                f"  {result['excerpt']}"
            )
        if not results:
            print("No matching meeting history found.")
        return

    print(history.answer(args.question, args.limit))


if __name__ == "__main__":
    main()
