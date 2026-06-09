import os
import re
from flask import Flask, request, jsonify
from slack_sdk import WebClient
from datetime import datetime

app = Flask(__name__)

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
CANVAS_ID = os.environ.get("CANVAS_ID")
CHANNEL_ID = os.environ.get("CHANNEL_ID")

slack_client = WebClient(token=SLACK_TOKEN)
processed_events = set()

def parse_message(text):
    result = {}

    # 오더번호 추출
    order_pattern = r'26\d{4}_[A-Za-z가-힣]+_[A-Za-z가-힣0-9]+'
    orders = re.findall(order_pattern, text)
    result['order_number'] = ' / '.join(orders) if orders else '-'

    # 출고일 추출
    date_str = None
    patterns = [
        (r'(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})', 'ymd'),
        (r'(\d{2})\.(\d{2})\.(\d{2})', 'ymd2'),
        (r'(\d{1,2})월\s*(\d{1,2})일', 'md'),
        (r'6\/(\d{1,2})', 'slash'),
    ]
    for pattern, ptype in patterns:
        match = re.search(pattern, text)
        if match:
            g = match.groups()
            if ptype == 'ymd':
                date_str = f"{g[0]}-{g[1].zfill(2)}-{g[2].zfill(2)}"
            elif ptype == 'ymd2':
                date_str = f"20{g[0]}-{g[1].zfill(2)}-{g[2].zfill(2)}"
            elif ptype == 'md':
                date_str = f"2026-{g[0].zfill(2)}-{g[1].zfill(2)}"
            elif ptype == 'slash':
                date_str = f"2026-06-{g[0].zfill(2)}"
            break
    result['date'] = date_str

    # 수량 추출
    qty_pattern = r'(\d{1,3}(?:,\d{3})*|\d+)\s*[Ee][Aa]'
    quantities = re.findall(qty_pattern, text)
    result['quantity'] = '+'.join(quantities) + 'EA' if quantities else '-'

    # 브랜드/건명 추출
    brand = '-'
    for pattern in [r'#\s*([^\n*]+?)\s*(?:출고|픽업|납품)', r'\*#([^\n*]+?)\*']:
        match = re.search(pattern, text)
        if match:
            brand = match.group(1).strip()
            break
    result['brand'] = brand

    # 도착시간 추출
    time_match = re.search(r'(?:오전|오후)\s*(\d{1,2}시)', text)
    result['arrival_time'] = time_match.group(0) if time_match else '-'

    return result

def get_weekday(date_str):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return ["(월)", "(화)", "(수)", "(목)", "(금)", "(토)", "(일)"][dt.weekday()]
    except:
        return ""

def update_canvas(parsed):
    date = parsed.get('date')
    if not date:
        print("날짜 파싱 실패")
        return

    weekday = get_weekday(date)
    section_title = f"📦 {date} {weekday}"
    new_content = f"\n## {section_title}\n\n| 브랜드 / 건명 | 수량 | 도착시간 | 오더번호 |\n|---|---|---|---|\n|{parsed['brand']}|{parsed['quantity']}|{parsed['arrival_time']}|{parsed['order_number']}|\n"

    try:
        slack_client.canvases_update(
            canvas_id=CANVAS_ID,
            changes=[{
                "operation": "insert_after",
                "document_content": {
                    "type": "markdown",
                    "markdown": new_content
                }
            }]
        )
        print(f"Canvas 업데이트 성공: {section_title}")
    except Exception as e:
        print(f"Canvas 업데이트 오류: {e}")

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
        print(f"메시지 수신: channel={channel}, text={text[:50]}")

        if channel == CHANNEL_ID and "출고" in text:
            parsed = parse_message(text)
            print(f"파싱 결과: {parsed}")
            update_canvas(parsed)

    return "OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
