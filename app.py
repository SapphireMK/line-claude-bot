import os
import re
import hmac
import hashlib
import base64
import datetime
from flask import Flask, request, abort
import requests
import psycopg2
import psycopg2.extras

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

MODEL = "claude-sonnet-4-6"
MAX_FILE_CHARS = 40000        # max chars of an uploaded .txt to store
TRIGGER = "ai:"               # wake-word for the assistant
DEFAULT_SUMMARY_DAYS = 7      # /summary with no date covers this many days
CONTEXT_MSG_LIMIT = 40        # how many recent msgs the ai: assistant sees
MAX_ROWS = 4000               # safety cap on rows pulled for one summary


# ---------- Database helpers ----------

def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id BIGSERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_chat_time "
            "ON messages (chat_id, created_at);"
        )
        conn.commit()


def store_message(chat_id: str, sender: str, content: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (chat_id, sender, content) VALUES (%s, %s, %s)",
            (chat_id, sender, content),
        )
        conn.commit()


def fetch_since(chat_id: str, since: datetime.datetime):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT sender, content FROM messages "
            "WHERE chat_id = %s AND created_at >= %s "
            "ORDER BY created_at ASC LIMIT %s",
            (chat_id, since, MAX_ROWS),
        )
        return cur.fetchall()


def fetch_recent(chat_id: str, limit: int):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT sender, content FROM messages "
            "WHERE chat_id = %s ORDER BY created_at DESC LIMIT %s",
            (chat_id, limit),
        )
        rows = cur.fetchall()
    return list(reversed(rows))


def search_messages(chat_id: str, keyword: str, limit: int = 30):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT sender, content, created_at FROM messages "
            "WHERE chat_id = %s AND content ILIKE %s "
            "ORDER BY created_at DESC LIMIT %s",
            (chat_id, f"%{keyword}%", limit),
        )
        return cur.fetchall()


# ---------- LINE / Claude helpers ----------

def verify_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def reply_to_line(reply_token: str, text: str):
    requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        },
        json={
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text[:5000]}],
        },
    )


def get_line_content(message_id: str) -> bytes:
    resp = requests.get(
        f"https://api-data.line.me/v2/bot/message/{message_id}/content",
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
    )
    resp.raise_for_status()
    return resp.content


def call_claude(system: str, user_content: str) -> str:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": user_content}],
        },
    )
    data = resp.json()
    return "".join(
        b["text"] for b in data.get("content", []) if b.get("type") == "text"
    )


def summarize(rows) -> str:
    convo = "\n".join(f"{s}: {t}" for s, t in rows)
    system = (
        "You summarize LINE chat history for a busy reader. Content may be a "
        "work conversation or documents people pasted/uploaded. Always fill Key "
        "Points with the real substance. Keep it concise."
    )
    user = (
        "Summarize the content below in exactly this format:\n\n"
        "## Summary\n(2-4 sentence overview)\n\n"
        "## Key Points\n- (most important information)\n\n"
        "## Action Items\n- (who needs to do what; 'None' if none)\n\n"
        "## Deadlines\n- (any dates; 'None' if none)\n\n"
        "--- CONTENT START ---\n"
        f"{convo}\n"
        "--- CONTENT END ---"
    )
    return call_claude(system, user)


def assistant_reply(history_rows, question: str) -> str:
    context = "\n".join(f"{s}: {t}" for s, t in history_rows)
    system = (
        "You are a helpful assistant inside a LINE work group. Answer the "
        "user's latest request clearly and concisely. Use the recent chat "
        "history for context. You only know what is in the chat -- you have no "
        "access to company systems. If you lack the info, say so briefly."
    )
    user = (
        "Recent chat history (for context):\n"
        "--- HISTORY START ---\n"
        f"{context}\n"
        "--- HISTORY END ---\n\n"
        f"The user just said: {question}\n\n"
        "Reply to the user."
    )
    return call_claude(system, user)


# ---------- Command parsing ----------

DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def parse_since_date(text: str):
    """Return a datetime if the command specifies 'since YYYY-MM-DD', else None."""
    m = DATE_RE.search(text)
    if not m:
        return None
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return datetime.datetime(y, mo, d, tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


@app.route("/")
def health():
    return "OK"


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()
    if not verify_signature(body, signature):
        abort(400)

    payload = request.get_json()
    for event in payload.get("events", []):
        if event.get("type") != "message":
            continue

        source = event["source"]
        chat_id = (
            source.get("groupId")
            or source.get("roomId")
            or source.get("userId")
        )
        sender = source.get("userId", "unknown")
        reply_token = event["replyToken"]
        message = event["message"]
        msg_type = message.get("type")

        if msg_type == "text":
            text = message["text"]
            stripped = text.strip()
            lower = stripped.lower()

            # /summary  or  /summary since YYYY-MM-DD
            if lower.startswith("/summary") or lower.startswith("สรุป"):
                since = parse_since_date(stripped)
                if since is None:
                    since = datetime.datetime.now(datetime.timezone.utc) - \
                        datetime.timedelta(days=DEFAULT_SUMMARY_DAYS)
                rows = fetch_since(chat_id, since)
                if not rows:
                    reply_to_line(
                        reply_token,
                        "No messages found for that period yet.",
                    )
                else:
                    reply_to_line(reply_token, summarize(rows))

            # /find <keyword>  -> search past messages
            elif lower.startswith("/find"):
                keyword = stripped[len("/find"):].strip()
                if not keyword:
                    reply_to_line(reply_token, "Usage: /find <keyword>")
                else:
                    hits = search_messages(chat_id, keyword)
                    if not hits:
                        reply_to_line(
                            reply_token, f"No past messages mention '{keyword}'."
                        )
                    else:
                        lines = []
                        for s, c, ts in hits:
                            date = ts.strftime("%Y-%m-%d %H:%M")
                            snippet = c if len(c) <= 120 else c[:120] + "…"
                            lines.append(f"[{date}] {snippet}")
                        out = f"Messages mentioning '{keyword}':\n\n" + \
                            "\n".join(lines)
                        reply_to_line(reply_token, out)

            # ai: <question>  -> conversational assistant
            elif lower.startswith(TRIGGER):
                question = stripped[len(TRIGGER):].strip()
                history = fetch_recent(chat_id, CONTEXT_MSG_LIMIT)
                answer = assistant_reply(history, question)
                store_message(chat_id, sender, text)
                store_message(chat_id, "assistant", answer)
                reply_to_line(reply_token, answer)

            # normal message -> just store it
            else:
                store_message(chat_id, sender, text)

        elif msg_type == "file":
            file_name = message.get("fileName", "file")
            if file_name.lower().endswith(".txt"):
                try:
                    content = get_line_content(message["id"])
                    text = content.decode("utf-8", errors="replace")[:MAX_FILE_CHARS]
                    store_message(chat_id, f"[file: {file_name}]", text)
                except Exception:
                    pass

    return "OK"


# Initialise the database table on startup.
try:
    init_db()
except Exception as e:
    print("DB init error:", e)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
