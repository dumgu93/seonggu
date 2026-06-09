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
    # 멘션/링크 제거
    text = re.sub(r'<@[A-Z0-9]+\|[^>]+>', '', text)
    text = re.sub(r'<@[A-Z0-9]+>', '', text)
    text = re.sub(r'<!subteam\^[^>]+>', '', text)
    text = re.sub(r'<http[^>]+>', '', text)
    # 이모지 제거 (그림 이모지 + 변형 선택자)
    emoji_pattern = re.compile(
        "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U0000FE00-\U0000FE0F\U00002190-\U000021FF\U00002B00-\U00002BFF]+",
        flags=re.UNICODE
    )
    text = emoji_pattern.sub('', text)
    # :emoji_code: 형식 제거
    text = re.sub(r':[a-z_]+:', '', text)
    # 대괄호 표시 제거 (예: [긴급])
    text = re.sub(r'\[[^\]]*\]', '', text)
    return text

def parse_message(text):
    result = {}
    clean_text = clean(text)

    # 오더번호 추출
    order_pattern = r'26\d{4}_[A-Za-z가-힣]+_[A-Za-z가-힣0-9()]+(?:\([^)]+\))?'
    orders = re.findall(order_pattern, clean_text)
    result['order_number'] = ' / '.join(orders) if orders else '-'

    # 날짜 추출 - 다양한 형식 지원
    date_str = None
    patterns = [
        (r'6/(\d{1,2})일?', 'slash6'),
        (r'6월\s*(\d{1,2})일?', 'month6'),
        (r'(\d{1,2})월\s*(\d{1,2})일?', 'md'),
        (r'(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})', 'ymd'),
        (r'26\.(\d{2})\.(\d{2})', 'short'),
    ]
    for pattern, ptype in patterns:
        match = re.search(pattern, clean_text)
        if match:
            g = match.groups()
            if ptype == 'slash6':
                date_str = f"2026-06-{g[0].zfill(2)}"
            elif ptype == 'month6':
                date_str = f"2026-06-{g[0].zfill(2)}"
            elif ptype == 'md':
                date_str = f"2026-{g[0].zfill(2)}-{g[1].zfill(2)}"
            elif ptype == 'ymd':
                date_str = f"{g[0]}-{g[1].zfill(2)}-{g[2].zfill(2)}"
            elif ptype == 'short':
                date_str = f"20{g[0]}-{g[1].zfill(2)}"
            break
    result['date'] = date_str

    # 수량 추출
    qty_pattern = r'(\d{1,3}(?:,\d{3})*|\d+)\s*[Ee][Aa]'
    quantities = re.findall(qty_pattern, clean_text)
    result['quantity'] = '+'.join(quantities) + 'EA' if quantities else '-'

    # 브랜드/건명 추출 (출고 요청 / 출고요청 둘 다 인식)
    brand = '-'
    for pattern in [
        r'#\s*(베리시[^\n*@<(]+?)(?:\s*출고\s*요청|\s*출고요청|$|\n)',
        r'#\s*([^\n*@<(]+?)\s*출고\s*요청',
        r'#\s*([^\n*@<(]+?)\s*출고요청',
    ]:
        match = re.search(pattern, clean_text)
        if match:
            brand = match.group(1).strip()
            break
    result['brand'] = brand

    # 도착시간 추출
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
        result = slack_client.conversations_history(
            channel=CHANNEL_ID,
            limit=200
        )
        messages = result.get("messages", [])

        parsed_list = []
        for msg in messages:
            text = msg.get("text", "")
            if "출고" in text:
                parsed = parse_message(text)
                if parsed.get("date"):
                    parsed_list.append(parsed)

        if not parsed_list:
            print("파싱된 메시지 없음")
            return

        def sort_key(p):
            date = p.get('date', '9999-99-99')
            time_str = p.get('arrival_time', '')
            hour = 99
            match = re.search(r'(\d{1,2})시', time_str)
            if match:
                hour = int(match.group(1))
                if '오후' in time_str and hour != 12:
                    hour += 12
            return (date, hour)

        parsed_list.sort(key=sort_key)

        cal_result = slack_client.conversations_history(
            channel=CALENDAR_CHANNEL_ID,
            limit=200
        )
        for msg in cal_result.get("messages", []):
            try:
                slack_client.chat_delete(
                    channel=CALENDAR_CHANNEL_ID,
                    ts=msg["ts"]
                )
            except Exception as e:
                print(f"메시지 삭제 오류: {e}")

        from itertools import groupby
        for date, group in groupby(parsed_list, key=lambda x: x['date']):
            weekday = get_weekday(date)
            items = list(group)

            lines = [f"*📦 {date} {weekday}*"]
            lines.append("```")
            lines.append(f"{'브랜드/건명':<25} {'수량':<15} {'도착시간':<12} {'오더번호'}")
            lines.append("-" * 80)
            for item in items:
                lines.append(f"{item['brand']:<25} {item['quantity']:<15} {item['arrival_time']:<12} {item['order_number']}")
            lines.append("```")

            slack_client.chat_postMessage(
                channel=CALENDAR_CHANNEL_ID,
                text="\n".join(lines)
            )

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
                message = f"*📦 {parsed['date']} {weekday}*\n*브랜드/건명:* {parsed['brand']}\n*수량:* {parsed['quantity']}\n*도착시간:* {parsed['arrival_time']}\n*오더번호:* {parsed['order_number']}"
                try:
                    slack_client.chat_postMessage(
                        channel=CALENDAR_CHANNEL_ID,
                        text=message
                    )
                    print("즉시 포스팅 성공")
                except Exception as e:
                    print(f"즉시 포스팅 오류: {e}")

    return "OK"

@app.route("/trigger-sort", methods=["GET"])
def trigger_sort():
    collect_and_sort()
    return "정렬 완료!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
