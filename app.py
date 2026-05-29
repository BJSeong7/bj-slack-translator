import os
import hmac
import hashlib
import time
import threading
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

SLACK_BOT_TOKEN       = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET  = os.environ["SLACK_SIGNING_SECRET"]
GROQ_API_KEY          = os.environ["GROQ_API_KEY"]
BJ_USER_ID            = os.environ.get("BJ_USER_ID", "U0B6K2GG7PX")
BJ_SLACK_ID           = os.environ.get("BJ_SLACK_ID", "U09D2V6EQBD")
EUGENE_SUPPORT_CH     = os.environ.get("EUGENE_SUPPORT_CHANNEL", "C07P7V49S6B")

KO_PROMPT = """You are translating Slack messages from English-speaking developers into Korean for a Korean business professional.

Rules:
- Natural, readable Korean — not robotic machine translation
- Keep technical terms in English (e.g. UAT, XSP, API, Mixpanel)
- Keep names and usernames as-is
- Keep emojis
- Short and clear

Return only the Korean translation, nothing else."""

EN_PROMPT = """You are translating messages from a Korean business professional into natural English.

Rules:
- No hyphens (— or -) to connect phrases. Use separate sentences instead.
- No AI filler: no "I wanted to reach out", "We'd appreciate it if", "please don't hesitate"
- Use present tense when natural ("is asking" not "has requested")
- Short, clean sentences. Keep any emojis.
- Sound like a person, not a formal letter

Good example:
Korean: "카카오 측에서 6월 5일부터 매주 금요일 릴리즈 예정 항목을 정리해서 공유해달라고 요청하고 있습니다."
Good: "Kakao is asking us to organize and share the items planned for release every Friday starting June 5. 🙏"

Return only the English translation, nothing else."""


# ── 이미 보낸 메시지 추적 (ts 기준) ─────────────────────────
seen_ts = set()
# BJ가 참여한 스레드 추적 (thread_ts -> reply_count)
bj_threads = {}


def groq(system_prompt, text):
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
                "max_tokens": 1024,
            },
            timeout=30,
        )
        if r.ok:
            return r.json()["choices"][0]["message"]["content"].strip()
        print(f"[Groq 오류] {r.status_code}: {r.text[:200]}")
        return None
    except Exception as e:
        print(f"[Groq 예외] {e}")
        return None


def send_dm(text):
    requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={"channel": BJ_USER_ID, "text": text},
        timeout=10,
    )


def slack_get(endpoint, params):
    r = requests.get(
        f"https://slack.com/api/{endpoint}",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        params=params,
        timeout=10,
    )
    return r.json() if r.ok else {}


def get_user_name(user_id):
    data = slack_get("users.info", {"user": user_id})
    profile = data.get("user", {}).get("profile", {})
    return profile.get("display_name") or profile.get("real_name") or user_id


def format_and_send(msg, thread_title=None):
    user_id  = msg.get("user", "")
    ts       = msg.get("ts", "")
    text     = msg.get("text", "").strip()
    thread_ts = msg.get("thread_ts")

    if not text or text.startswith("다음을 사용하여"):
        return

    # 봇 메시지 무시
    if msg.get("bot_id") or msg.get("subtype") == "bot_message":
        return

    sender   = get_user_name(user_id) if user_id else "미확인"
    kst_time = time.strftime("%Y-%m-%d %H:%M KST", time.localtime(float(ts)))
    link     = f"https://optionshub.slack.com/archives/{EUGENE_SUPPORT_CH}/p{ts.replace('.', '')}"

    translated = groq(KO_PROMPT, text)
    if not translated:
        return

    title = f"📌 스레드: {thread_title}" if thread_title else "📌 새 스레드"
    dm = (
        f"[#eugene-support 번역]\n\n"
        f"──────────────────────\n"
        f"보낸 사람: {sender}\n"
        f"시간: {kst_time}\n"
        f"원문 링크: {link}\n"
        f"{title}\n\n"
        f"{translated}\n"
        f"──────────────────────"
    )
    send_dm(dm)


def poll_eugene_support():
    global seen_ts, bj_threads
    time.sleep(10)  # 서버 시작 후 잠깐 대기
    print("[폴링] eugene-support 모니터링 시작")

    while True:
        try:
            oldest = str(time.time() - 300)  # 최근 5분

            # ── 1. 새 최상위 메시지 (새 스레드) ──────────────
            data = slack_get("conversations.history", {
                "channel": EUGENE_SUPPORT_CH,
                "limit": 10,
                "oldest": oldest,
            })
            for msg in data.get("messages", []):
                ts = msg.get("ts", "")
                if ts in seen_ts:
                    continue
                seen_ts.add(ts)

                # BJ 본인 메시지는 번역 안 함
                if msg.get("user") == BJ_SLACK_ID:
                    # BJ가 보낸 스레드는 추적 목록에 추가
                    bj_threads[ts] = int(msg.get("reply_count", 0))
                    continue

                # 새 메시지 번역 후 DM
                format_and_send(msg)

            # ── 2. BJ 참여 스레드의 새 댓글 ──────────────────
            for thread_ts, last_count in list(bj_threads.items()):
                thread_data = slack_get("conversations.replies", {
                    "channel": EUGENE_SUPPORT_CH,
                    "ts": thread_ts,
                    "limit": 5,
                    "oldest": oldest,
                })
                msgs = thread_data.get("messages", [])
                parent = msgs[0] if msgs else {}
                current_count = int(parent.get("reply_count", last_count))

                if current_count > last_count:
                    bj_threads[thread_ts] = current_count
                    # 새 댓글들만 처리
                    for reply in msgs[1:]:
                        r_ts = reply.get("ts", "")
                        if r_ts in seen_ts:
                            continue
                        seen_ts.add(r_ts)
                        if reply.get("user") == BJ_SLACK_ID:
                            continue
                        parent_text = parent.get("text", "")[:30]
                        format_and_send(reply, thread_title=parent_text)

        except Exception as e:
            print(f"[폴링 오류] {e}")

        time.sleep(180)  # 3분마다


# ── 백그라운드 폴링 스레드 시작 ──────────────────────────────
threading.Thread(target=poll_eugene_support, daemon=True).start()


# ── Slack 이벤트 웹훅 (DM 번역) ──────────────────────────────
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


@app.route("/slack/events", methods=["POST"])
def slack_events():
    if not verify_slack(request):
        return jsonify({"error": "invalid signature"}), 403

    data  = request.get_json(force=True)
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    event = data.get("event", {})
    if (
        event.get("type") == "message"
        and event.get("channel_type") == "im"
        and event.get("user") == BJ_SLACK_ID
        and not event.get("bot_id")
        and not event.get("subtype")
    ):
        text = event.get("text", "").strip()
        if text:
            translated = groq(EN_PROMPT, text)
            channel    = event["channel"]
            requests.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                json={"channel": channel, "text": f"🇺🇸 {translated}" if translated else "❌ 번역 실패"},
                timeout=10,
            )

    return jsonify({"ok": True})


@app.route("/test-poll", methods=["GET"])
def test_poll():
    """최근 48시간 메시지를 강제로 스캔해서 BJ에게 DM 발송 (테스트용)"""
    oldest = str(time.time() - 172800)  # 48시간
    sent = []

    # 채널 히스토리
    data = slack_get("conversations.history", {
        "channel": EUGENE_SUPPORT_CH,
        "limit": 20,
        "oldest": oldest,
    })
    for msg in data.get("messages", []):
        ts = msg.get("ts", "")
        if msg.get("user") == BJ_SLACK_ID:
            # BJ 스레드 댓글 확인
            thread_data = slack_get("conversations.replies", {
                "channel": EUGENE_SUPPORT_CH,
                "ts": ts,
                "limit": 20,
            })
            msgs = thread_data.get("messages", [])
            parent = msgs[0] if msgs else {}
            for reply in msgs[1:]:
                if reply.get("user") == BJ_SLACK_ID:
                    continue
                if reply.get("bot_id") or reply.get("subtype") == "bot_message":
                    continue
                parent_text = parent.get("text", "")[:30]
                format_and_send(reply, thread_title=parent_text)
                sent.append(reply.get("ts"))
        else:
            if msg.get("bot_id") or msg.get("subtype") == "bot_message":
                continue
            format_and_send(msg)
            sent.append(ts)

    return jsonify({"ok": True, "sent": len(sent), "ts_list": sent})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
