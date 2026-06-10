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
    text = re.sub(r'<tel:[^>]+>', '', text)
    text = re.sub(r'<http[^>]+>', '', text)
    # 이모지 제거
    emoji_pattern = re.compile(
        "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U0000FE00-\U0000FE0F\U00002190-\U000021FF\U00002B00-\U00002BFF]+",
        flags=re.UNICODE
    )
    text = emoji_pattern.sub('', text)
    # :emoji_code: 제거
    text = re.sub(r':[a-z_]+:', '', text)
    # [긴급] 같은 대괄호 제거
    text = re.sub(r'\[[^\]]*\]', '', text)
    return text

def extract_date(text):
    """출고일 라인을 우선 찾고, 거기서 날짜를 추출"""
    # 1순위: '출고일' 또는 '도착일'이 있는 줄에서 날짜 찾기
    target_line = None
    for line in text.split('\n'):
        if '출고일' in line or '도착일' in line or '출고 요청일' in line:
            target_line = line
            break
    search_text = target_line if target_line else text

    # 날짜 패턴들 (우선순위 순)
    patterns = [
        (r'26년\s*(\d{1,2})월\s*(\d{1,2})일', 'ymd_kr'),   # 26년6월16일
        (r'(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})', 'ymd'),    # 2026-06-13
        (r'26\.(\d{1,2})\.(\d{1,2})', 'short'),             # 26.06.13
        (r'(\d{1,2})\s*월\s*/?\s*(\d{1,2})\s*일', 'md'),    # 6월13일, 6월 13일
        (r'(\d{1,2})\s*/\s*(\d{1,2})\s*일?', 'slash'),       # 6/13, 6/13일
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
    """첫 번째 # 라인에서 브랜드명 추출"""
    brand = '-'
    # # 으로 시작하는 첫 줄 찾기
    for line in text.split('\n'):
        line = line.strip().lstrip('*').strip()
        if line.startswith('#'):
            # # 제거
            content = line.lstrip('#').strip()
            # '출고 요청', '출고요청', '출고', '요청', '퀵', '6차' 등 뒤쪽 키워드 제거
            content = re.sub(r'\s*(퀵\s*)?출고\s*요청.*$', '', content)
            content = re.sub(r'\s*(퀵\s*)?출고.*$', '', content)
            content = re.sub(r'\s*\d+월\s*\d+차.*$', '', content)  # '6월6차' 제거
            content = re.sub(r'\s*\d+차.*$', '', content)           # '4차' 제거
            content = re.sub(r'\s*퀵$', '', content)
            content = re.sub(r'\*', '', content)
            brand = content.strip()
            break
    return brand if brand else '-'

def parse_message(text):
    result = {}
    clean_text = clean(text)

    # 오더번호
    order_pattern = r'26\d{4}_[A-Za-z가-힣]+_[A-Za-z가-힣0-9()]+(?:\([^)]+\))?'
    orders = re.findall(order_pattern, clean_text)
    result['order_number'] = ' / '.join(orders) if orders else '-'

    # 날짜
    result['date'] = extract_date(clean_text)

    # 수량
    qty_pattern = r'(\d{1,3}(?:,\d{3})*|\d+)\s*[Ee][Aa]'
    quantities = re.findall(qty_pattern, clean_text)
    result['quantity'] = '+'.join(quantities) + 'EA' if quantities else '-'

    # 브랜드
    result['brand'] = extract_brand(clean_text)

    # 도착시간
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
            lines.append(f"{'브랜드/건명':<25} {'수량':<15} {'도착시간':<12} {'오더번호'}")
            lines.append("-" * 80)
            for item in items:
                lines.append(f"{item['brand']:<25} {item['quantity']:<15} {item['arrival_time']:<12} {item['order_number']}")
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
                message = f"*📦 {parsed['date']} {weekday}*\n*브랜드/건명:* {parsed['brand']}\n*수량:* {parsed['quantity']}\n*도착시간:* {parsed['arrival_time']}\n*오더번호:* {parsed['order_number']}"
                try:
                    slack_client.chat_postMessage(channel=CALENDAR_CHANNEL_ID, text=message)
                    print("즉시 포스팅 성공")
                except Exception as e:
                    print(f"즉시 포스팅 오류: {e}")
    return "OK"

@app.route("/trigger-sort", methods=["GET"])
def trigger_sort():
    collect_and_sort()
    return "정렬 완료!"

@app.route("/ping", methods=["GET"])
def ping():
    return "alive"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
