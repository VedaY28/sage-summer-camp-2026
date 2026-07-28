#!/usr/bin/env python3
"""Extract all user/assistant messages from session DB into a markdown file."""
import sqlite3
import json
import textwrap
from pathlib import Path
from datetime import datetime

DB_PATH = "/home/veday28/.hermes/profiles/sage/state.db"
OUT_PATH = "/home/veday28/sage-summer-camp-2026/session_log.md"

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

# Get all sessions with their metadata
sessions = conn.execute("""
    SELECT id, source, title, started_at, ended_at, message_count, model,
           cwd, datetime(started_at, 'unixepoch') as started_str
    FROM sessions
    ORDER BY started_at ASC
""").fetchall()

print(f"Found {len(sessions)} sessions")

out = []
out.append("# SageAir Project — Full Session Log")
out.append("")
out.append("This file contains every user prompt and assistant response from all SageAir-related sessions.")
out.append("Generated from the Hermes session database.")
out.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
out.append("")
out.append("---")
out.append("")

total_user_msgs = 0
total_asst_msgs = 0

for sess in sessions:
    sid = sess["id"]
    title = sess["title"] or "(untitled)"
    started = sess["started_str"]
    msg_count = sess["message_count"] or 0
    cwd = sess["cwd"] or ""

    # Get user + assistant messages for this session
    messages = conn.execute("""
        SELECT id, role, content, tool_calls, tool_name, timestamp,
               datetime(timestamp, 'unixepoch') as ts_str
        FROM messages
        WHERE session_id = ? AND role IN ('user', 'assistant')
        ORDER BY id ASC
    """, (sid,)).fetchall()

    if not messages:
        continue

    # Check if this is a SageAir-related session (by cwd or content)
    is_sageair = False
    all_content = " ".join([(m["content"] or "") for m in messages])
    if "SageAir" in cwd or "sageair" in all_content.lower() or "air quality" in all_content.lower() or "pm25" in all_content.lower() or "w0a0" in all_content.lower() or "w095" in all_content.lower():
        is_sageair = True

    if not is_sageair:
        continue

    out.append(f"## Session: {title}")
    out.append(f"- **Session ID:** {sid}")
    out.append(f"- **Started:** {started}")
    out.append(f"- **Messages:** {msg_count}")
    out.append(f"- **Working dir:** {cwd}")
    out.append("")

    for msg in messages:
        role = msg["role"]
        content = msg["content"] or ""
        ts = msg["ts_str"] or ""
        tool_calls = msg["tool_calls"]
        tool_name = msg["tool_name"]

        if role == "user":
            total_user_msgs += 1
            # Skip empty or trivial messages
            content_stripped = content.strip()
            if not content_stripped:
                continue
            # Skip out-of-band markers (we just want the user's actual text)
            if content_stripped.startswith("[IMPORTANT:"):
                # Extract the actual message from background process notifications
                pass
            out.append(f"### [User] {ts}")
            out.append("")
            out.append(content_stripped)
            out.append("")
        elif role == "assistant":
            total_asst_msgs += 1
            if not content.strip() and not tool_calls:
                continue
            out.append(f"### [Assistant] {ts}")
            out.append("")
            if content.strip():
                out.append(content.strip())
                out.append("")
            # Note tool calls (summarized)
            if tool_calls:
                try:
                    tc = json.loads(tool_calls) if isinstance(tool_calls, str) else tool_calls
                    if isinstance(tc, list):
                        tool_names = []
                        for t in tc:
                            if isinstance(t, dict):
                                fn = t.get("function", {}).get("name", t.get("name", "?"))
                                tool_names.append(fn)
                        if tool_names:
                            out.append(f"*(Tool calls: {', '.join(tool_names)})*")
                            out.append("")
                except:
                    out.append("*(Tool calls made)*")
                    out.append("")

    out.append("---")
    out.append("")

conn.close()

# Write to file
Path(OUT_PATH).write_text("\n".join(out))
print(f"\nDone! Written to {OUT_PATH}")
print(f"Total user messages: {total_user_msgs}")
print(f"Total assistant messages: {total_asst_msgs}")
print(f"File size: {len(''.join(out)):,} chars")
