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

# Stores messages per chat in memory. Resets if the server restarts.
# Fine for getting started; swap for a database later if needed.
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


def summarize_with_claude(messages: list) -> str:
    convo = "\n".join(f"{s}: {t}" for s, t in messages)
    prompt = (
        "Summarize the following work group chat. Give a short summary, "
        "then a bullet list of action items, decisions, and any deadlines "
        "mentioned. If someone was asked to do something, note who.\n\n"
        f"{convo}"
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
        if event["type"] != "message" or event["message"]["type"] != "text":
            continue

        source = event["source"]
        chat_id = (
            source.get("groupId")
            or source.get("roomId")
            or source.get("userId")
        )
        text = event["message"]["text"]
        sender = source.get("userId", "unknown")
        reply_token = event["replyToken"]

        if text.strip().lower() in ("/summary", "สรุป"):
            msgs = message_log.get(chat_id, [])
            if not msgs:
                reply_to_line(reply_token, "No messages logged yet.")
            else:
                summary = summarize_with_claude(msgs)
                reply_to_line(reply_token, summary)
                message_log[chat_id] = []
        else:
            message_log[chat_id].append((sender, text))

    return "OK"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
