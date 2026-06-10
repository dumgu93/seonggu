import os
import re
from flask import Flask, request, jsonify
from slack_sdk import WebClient
from datetime import datetime
import threading
import time

app = Flask(__name__)

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
CALENDAR_CHANNEL_ID = "C0B92726KKM"

slack_client = WebClient(token=SLACK_TOKEN)
processed_events = set()

def clean(text):
    text = re.sub(r'<@[A-Z0-9]+\|[^>]+>', '', text)
    text = re.sub(r'<@[A-Z0-9]+>', '', text)
    text = re.sub(r'<!subteam\^[^>]+>', '', text)
    text = re.sub(r'<tel:[^>|]+\|([^>]+)>', r'\1', text)  # <tel:..|010-..> → 010-..
    text = re.sub(r'<tel:[^>]+>', '', text)
    text = re.sub(r'<http[^>]+>', '', text)
    emoji_pattern = re.compile(
        "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U0000FE00-\U0000FE0F\U00002190-\U000021FF\U00002B00-\U00002BFF]+",
        flags=re.UNICODE
    )
    text = emoji_pattern.sub('', text)
    text = re.sub(r':[a-z_]+:', '', text)
    text = re.sub(r'\[[^\]]*\]', '', text)
    return text

def extract_date(text):
    target_line = None
    for line in text.split('\n'):
        if '출고일' in line or '도착일' in line or '출고 요청일' in line or '출고요청일' in line:
            target_line = line
            break
    search_text = target_line if target_line else text

    patterns = [
        (r'26년\s*(\d{1,2})월\s*(\d{1,2})일', 'ymd_kr'),
        (r'(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})', 'ymd'),
        (r'26\.(\d{1,2})\.(\d{1,2})', 'short'),
        (r'(\d{1,2})\s*월\s*/?\s*(\d{1,2})\s*[일(]', 'md'),
        (r'(\d{1,2})\s*월\s*/?\s*(\d{1,2})', 'md'),
        (r'(\d{1,2})\s*/\s*(\d{1,2})\s*일?', 'slash'),
    ]
    for pattern, ptype in patterns:
        match = re.search(pattern, search_text)
        if match:
            g = match.groups()
            if ptype == 'ymd_kr':
                return f"2026-{g[0].zfill(2)}-{g[1].zfill(2)}"
            elif ptype == 'ymd':
                return f"{g[0]}-{g[1].zfill(2)}-{g[2].zfill(2)}"
            elif ptype == 'short':
                return f"2026-{g[0].zfill(2)}-{g[1].zfill(2)}"
            elif ptype == 'md':
                return f"2026-{g[0].zfill(2)}-{g[1].zfill(2)}"
            elif ptype == 'slash':
                return f"2026-{g[0].zfill(2)}-{g[1].zfill(2)}"
    return None

def extract_brand(text):
    brand = '-'
    for line in text.split('\n'):
        line_s = line.strip().lstrip('*').strip()
        if line_s.startswith('#'):
            content = line_s.lstrip('#').strip()
            content = re.sub(r'\s*(퀵\s*)?출고\s*요청.*$', '', content)
            content = re.sub(r'\s*(퀵\s*)?출고.*$', '', content)
            content = re.sub(r'\s*\d+월\s*\d+차.*$', '', content)
            content = re.sub(r'\s*\d+차.*$', '', content)
            content = re.sub(r'\s*퀵$', '', content)
            content = re.sub(r'\*', '', content)
            brand = content.strip()
            if brand:
                return brand

    m = re.search(r'([가-힣A-Za-z0-9]+(?:\s[가-힣A-Za-z0-9]+){0,3})\s*(?:퀵\s*)?출고\s*요청', text)
    if m:
        cand = m.group(1).strip()
        cand = re.sub(r'^(안녕하세요|안녕하십니까)\s*,?\s*', '', cand)
        cand = cand.split(',')[-1].strip()
        if cand:
            return cand

    return brand

def extract_driver(text):
    """수령지 연락처 / 배차 기사 정보 추출 (전화번호 위주)"""
    phones = re.findall(r'01[016789]-?\d{3,4}-?\d{4}', text)
    if phones:
        # 중복 제거, 최대 2개까지
        uniq = []
        for p in phones:
            if p not in uniq:
                uniq.append(p)
        return ' / '.join(uniq[:2])
    return '-'

def parse_message(text):
    result = {}
    clean_text = clean(text)

    order_pattern = r'26\d{4}_[A-Za-z가-힣]+_[A-Za-z가-힣0-9()]+(?:\([^)]+\))?'
    orders = re.findall(order_pattern, clean_text)
    result['order_number'] = ' / '.join(orders) if orders else '-'

    result['date'] = extract_date(clean_text)
    result['brand'] = extract_brand(clean_text)
    result['driver'] = extract_driver(clean_text)

    time_match = re.search(r'(?:오전|오후)\s*(\d{1,2}시(?:\s*\d{1,2}분)?)', clean_text)
    if time_match:
        result['arrival_time'] = time_match.group(0)
    elif '추후' in clean_text:
        result['arrival_time'] = '추후 업데이트'
    else:
        result['arrival_time'] = '-'

    return result

def get_weekday(date_str):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return ["(월)", "(화)", "(수)", "(목)", "(금)", "(토)", "(일)"][dt.weekday()]
    except:
        return ""

def collect_and_sort():
    print("오후 5시 정렬 시작!")
    try:
        result = slack_client.conversations_history(channel=CHANNEL_ID, limit=200)
        messages = result.get("messages", [])

        parsed_list = []
        for msg in messages:
            text = msg.get("text", "")
            if "출고" in text:
                parsed = parse_message(text)
                if parsed.get("date"):
                    parsed_list.append(parsed)

            # 스레드 댓글 처리 (기사 정보 보강 + 댓글 내 출고건)
            if msg.get("reply_count", 0) > 0:
                try:
                    replies = slack_client.conversations_replies(
                        channel=CHANNEL_ID, ts=msg["ts"], limit=50
                    )
                    # 원본에서 driver가 '-'면 댓글에서 전화번호 보강
                    reply_texts = []
                    for reply in replies.get("messages", []):
                        if reply.get("ts") == msg["ts"]:
                            continue
                        r_text = reply.get("text", "")
                        reply_texts.append(r_text)
                        # 댓글에 출고건이 따로 있으면 추가
                        if "출고" in r_text and ("출고일" in r_text or "출고오더" in r_text):
                            r_parsed = parse_message(r_text)
                            if r_parsed.get("date"):
                                parsed_list.append(r_parsed)

                    # 원본 건의 driver가 비어있으면 댓글 전화번호로 보강
                    if parsed_list and "출고" in text:
                        joined = "\n".join(reply_texts)
                        d = extract_driver(clean(joined))
                        if d != '-' and parsed_list[-1].get('driver', '-') == '-':
                            parsed_list[-1]['driver'] = d
                except Exception as e:
                    print(f"스레드 읽기 오류: {e}")

        if not parsed_list:
            print("파싱된 메시지 없음")
            return

        def sort_key(p):
            date = p.get('date', '9999-99-99')
            time_str = p.get('arrival_time', '')
            hour = 99
            m = re.search(r'(\d{1,2})시', time_str)
            if m:
                hour = int(m.group(1))
                if '오후' in time_str and hour != 12:
                    hour += 12
            return (date, hour)

        parsed_list.sort(key=sort_key)

        cal_result = slack_client.conversations_history(channel=CALENDAR_CHANNEL_ID, limit=200)
        for msg in cal_result.get("messages", []):
            try:
                slack_client.chat_delete(channel=CALENDAR_CHANNEL_ID, ts=msg["ts"])
            except Exception as e:
                print(f"메시지 삭제 오류: {e}")

        from itertools import groupby
        for date, group in groupby(parsed_list, key=lambda x: x['date']):
            weekday = get_weekday(date)
            items = list(group)
            lines = [f"*📦 {date} {weekday}*", "```"]
            lines.append(f"{'브랜드/건명':<22} {'도착시간':<12} {'오더번호':<28} {'기사정보'}")
            lines.append("-" * 90)
            for item in items:
                lines.append(f"{item['brand']:<22} {item['arrival_time']:<12} {item['order_number']:<28} {item.get('driver','-')}")
            lines.append("```")
            slack_client.chat_postMessage(channel=CALENDAR_CHANNEL_ID, text="\n".join(lines))

        print(f"정렬 완료! 총 {len(parsed_list)}건")
    except Exception as e:
        print(f"정렬 오류: {e}")

def scheduler():
    while True:
        now = datetime.now()
        if now.hour == 8 and now.minute == 0:
            collect_and_sort()
            time.sleep(60)
        time.sleep(30)

scheduler_thread = threading.Thread(target=scheduler, daemon=True)
scheduler_thread.start()

@app.route("/slack/events", methods=["POST"])
def slack_events():
    data = request.json
    if "challenge" in data:
        return jsonify({"challenge": data["challenge"]})

    event = data.get("event", {})
    event_id = data.get("event_id", "")
    if event_id in processed_events:
        return "OK"
    processed_events.add(event_id)

    if event.get("type") == "message" and not event.get("subtype"):
        channel = event.get("channel")
        text = event.get("text", "")
        print(f"메시지 수신: channel={channel}, text={text[:80]}")
        if channel == CHANNEL_ID and "출고" in text:
            parsed = parse_message(text)
            print(f"파싱 결과: {parsed}")
            if parsed.get('date'):
                weekday = get_weekday(parsed['date'])
                message = f"*📦 {parsed['date']} {weekday}*\n*브랜드/건명:* {parsed['brand']}\n*도착시간:* {parsed['arrival_time']}\n*오더번호:* {parsed['order_number']}\n*기사정보:* {parsed.get('driver','-')}"
                try:
                    slack_client.chat_postMessage(channel=CALENDAR_CHANNEL_ID, text=message)
                    print("즉시 포스팅 성공")
                except Exception as e:
                    print(f"즉시 포스팅 오류: {e}")
    return "OK"

@app.route("/trigger-sort", methods=["GET"])
def trigger_sort():
    # 백그라운드로 실행 (타임아웃 방지)
    t = threading.Thread(target=collect_and_sort, daemon=True)
    t.start()
    return "정렬 시작! 1~2분 후 캘린더 채널을 확인해주세요."

@app.route("/ping", methods=["GET"])
def ping():
    return "alive"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
