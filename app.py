import os
import hmac
import hashlib
import time
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

SLACK_BOT_TOKEN      = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
GROQ_API_KEY         = os.environ["GROQ_API_KEY"]

TONE_PROMPT = """You are translating messages from a Korean business professional into natural English.

Write like a well-educated person who speaks English fluently — not like a translator.

Rules:
- No hyphens (— or -) to connect phrases. Use separate sentences instead.
- No AI-sounding filler: no "I wanted to reach out", "I hope this finds you well", "please don't hesitate"
- Short, clean sentences. Direct and confident.
- Use contractions naturally (I'll, we're, it's, that's)
- Sound human, not formal or stiff

Good example:
"No need to add extra comments. Feel free to use it mainly for prioritization. Leave comments only when necessary.
The main reason we're using this sheet is that Slack threads get hard to follow when they're too long."

Return only the English translation, nothing else."""


def verify_slack(req):
    ts  = req.headers.get("X-Slack-Request-Timestamp", "")
    sig = req.headers.get("X-Slack-Signature", "")
    if not ts or abs(time.time() - int(ts)) > 300:
        return False
    body     = req.get_data(as_text=True)
    base     = f"v0:{ts}:{body}"
    computed = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), base.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed, sig)


def translate_ko_to_en(text):
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": TONE_PROMPT},
                    {"role": "user", "content": text},
                ],
                "max_tokens": 1024,
            },
            timeout=30,
        )
        if r.ok:
            return r.json()["choices"][0]["message"]["content"].strip()
        err_msg = r.json().get("error", {}).get("message", r.text[:200])
        print(f"[Groq 오류] {r.status_code}: {err_msg}")
        return f"❌ {r.status_code}: {err_msg}"
    except Exception as e:
        print(f"[번역 오류] {e}")
        return None



def send_dm(channel, text):
    requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={"channel": channel, "text": text},
        timeout=10,
    )


@app.route("/slack/events", methods=["POST"])
def slack_events():
    if not verify_slack(request):
        return jsonify({"error": "invalid signature"}), 403

    data = request.get_json(force=True)

    # Slack URL 인증
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    event = data.get("event", {})

    # DM 메시지만 처리, 봇 자신 메시지 무시
    if (
        event.get("type") == "message"
        and event.get("channel_type") == "im"
        and not event.get("bot_id")
        and not event.get("subtype")
    ):
        text = event.get("text", "").strip()
        if text:
            translated = translate_ko_to_en(text)
            if translated:
                send_dm(event["channel"], f"🇺🇸 {translated}")
            else:
                send_dm(event["channel"], "❌ 번역 실패")

    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
