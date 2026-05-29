import os
import hmac
import hashlib
import time
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

SLACK_BOT_TOKEN      = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
GEMINI_API_KEY       = os.environ["GEMINI_API_KEY"]

TONE_PROMPT = """You are a professional translator. Translate the Korean text below into English.

Tone: Professional yet warm and friendly — like a colleague you've worked with for a while.
Natural business English, not overly formal. Concise and direct.

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
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
                json={"contents": [{"parts": [{"text": f"{TONE_PROMPT}\n\n{text}"}]}]},
                timeout=30,
            )
            if r.ok:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            print(f"[Gemini 오류] {r.status_code}: {r.text[:300]}")
            return f"❌ 번역 실패 ({r.status_code})"
        except Exception as e:
            print(f"[번역 오류] {e}")
            return None
    return "❌ 번역 실패 (한도 초과, 잠시 후 다시 시도해주세요)"



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
