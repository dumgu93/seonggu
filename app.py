import os
import re
from flask import Flask, request, jsonify
from slack_sdk import WebClient
from datetime import datetime

app = Flask(__name__)

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
CALENDAR_CHANNEL_ID = "C0B92726KKM"

slack_client = WebClient(token=SLACK_TOKEN)
processed_events = set()

def parse_message(text):
    result = {}

    # 멘션 제거
    clean_text = re.sub(r'<@[A-Z0-9]+\|[^>]+>', '', text)
    clean_text = re.sub(r'<!subteam\^[^>]+>', '', clean_text)
    clean_text = re.sub(r'<http[^>]+>', '', clean_text)

    # 오더번호 추출
    order_pattern = r'26\d{4}_[A-Za-z가-힣]+_[A-Za-z가-힣0-9()]+(?:\([^)]+\))?'
    orders = re.findall(order_pattern, clean_text)
    result['order_number'] = ' / '.join(orders) if orders else '-'

    # 날짜 추출 - 6/13 형식 우선
    date_str = None
    patterns = [
        (r'6/(\d{1,2})', 'slash6'),
        (r'(\d{1,2})월\s*/?\s*(\d{1,2})', 'md'),
        (r'(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})', 'ymd'),
    ]
    for pattern, ptype in patterns:
        match = re.search(pattern, clean_text)
        if match:
            g = match.groups()
            if ptype == 'slash6':
                date_str = f"2026-06-{g[0].zfill(2)}"
            elif ptype == 'md':
                date_str = f"2026-{g[0].zfill(2)}-{g[1].zfill(2)}"
            elif ptype == 'ymd':
                date_str = f"{g[0]}-{g[1].zfill(2)}-{g[2].zfill(2)}"
            break
    result['date'] = date_str

    # 수량 추출
    qty_pattern = r'(\d{1,3}(?:,\d{3})*|\d+)\s*[Ee][Aa]'
    quantities = re.findall(qty_pattern, clean_text)
    result['quantity'] = '+'.join(quantities) + 'EA' if quantities else '-'

    # 브랜드/건명 추출
    brand = '-'
    for pattern in [
        r'#\s*(베리시[^\n*@<(]+?)(?:\s*출고|$|\n)',
        r'#\s*([^\n*@<(]+?)\s*출고요청',
        r'#\s*([^\n*@<(]+?)\s*출고',
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

def post_to_calendar(parsed):
    date = parsed.get('date')
    if not date:
        print("날짜 파싱 실패")
        return

    weekday = get_weekday(date)

    message = f"""*📦 {date} {weekday} 출고 요청*

*브랜드/건명:* {parsed['brand']}
*수량:* {parsed['quantity']}
*도착시간:* {parsed['arrival_time']}
*오더번호:* {parsed['order_number']}
"""

    try:
        slack_client.chat_postMessage(
            channel=CALENDAR_CHANNEL_ID,
            text=message
        )
        print(f"캘린더 채널 포스팅 성공: {date}")
    except Exception as e:
        print(f"포스팅 오류: {e}")

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
            post_to_calendar(parsed)

    return "OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
