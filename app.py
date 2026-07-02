import os
import hmac
import hashlib
import base64
from collections import defaultdict
from flask import Flask, request, abort
import requests
 
app = Flask(__name__)
 
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
 
# Max characters of file text to include, to stay within model limits.
MAX_FILE_CHARS = 40000
 
# Stores messages per chat in memory. Resets if the server restarts.
# Each entry is (sender, text). Uploaded file contents are stored the same way.
message_log = defaultdict(list)
 
 
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
    """Download the binary content of a file/image message from LINE."""
    resp = requests.get(
        f"https://api-data.line.me/v2/bot/message/{message_id}/content",
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
    )
    resp.raise_for_status()
    return resp.content
 
 
def summarize_with_claude(messages: list) -> str:
    convo = "\n".join(f"{s}: {t}" for s, t in messages)
    prompt = (
        "Below is content collected from a LINE chat. It may be a work "
        "conversation between multiple people, or it may be a single document, "
        "article, or file that someone pasted or uploaded. Summarize it "
        "usefully for a busy reader.\n\n"
        "Format your reply exactly like this:\n\n"
        "## Summary\n"
        "A 2-4 sentence overview of what the content is about.\n\n"
        "## Key Points\n"
        "- Bullet points of the most important information, facts, or topics.\n\n"
        "## Action Items\n"
        "- Anything someone needs to do, and who. Write 'None' if there are none.\n\n"
        "## Deadlines\n"
        "- Any dates or deadlines mentioned. Write 'None' if there are none.\n\n"
        "Always fill in Key Points with the actual substance of the content. "
        "Do not spend the reply explaining what type of content it is; just "
        "summarize it. Keep it concise.\n\n"
        "--- CONTENT START ---\n"
        f"{convo}\n"
        "--- CONTENT END ---"
    )
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    data = resp.json()
    return "".join(
        block["text"] for block in data["content"] if block.get("type") == "text"
    )
 
 
def is_summary_command(text: str) -> bool:
    """Trigger on /summary or Thai สรุป, even with extra words after it."""
    t = text.strip().lower()
    return t.startswith("/summary") or t.startswith("สรุป")
 
 
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
 
        # Handle text messages
        if msg_type == "text":
            text = message["text"]
 
            if is_summary_command(text):
                msgs = message_log.get(chat_id, [])
                if not msgs:
                    reply_to_line(
                        reply_token,
                        "No messages or files logged yet. Send some "
                        "messages or upload a .txt file, then try /summary.",
                    )
                else:
                    summary = summarize_with_claude(msgs)
                    reply_to_line(reply_token, summary)
                    message_log[chat_id] = []
            else:
                message_log[chat_id].append((sender, text))
 
        # Handle uploaded files (e.g. .txt)
        elif msg_type == "file":
            file_name = message.get("fileName", "file")
            if file_name.lower().endswith(".txt"):
                try:
                    content = get_line_content(message["id"])
                    text = content.decode("utf-8", errors="replace")[:MAX_FILE_CHARS]
                    message_log[chat_id].append((f"[file: {file_name}]", text))
                except Exception:
                    # If download/decoding fails, just skip it silently.
                    pass
            # Non-.txt files are ignored (can't read binary formats here).
 
    return "OK"
 
 
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
