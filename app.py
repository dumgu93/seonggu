import os
import re
from flask import Flask, request
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
    date_patterns = [
        r'(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})',
        r'(\d{1,2})월\s*(\d{1,2})일',
        r'(\d{2})\.(\d{2})\.(\d{2})',
    ]
    date_str = None
    for pattern in date_patterns:
        match = re.search(pattern, text)
        if match:
            groups = match.groups()
            if len(groups) == 3 and len(groups[0]) == 4:
                date_str = f"{groups[0]}-{groups[1].zfill(2)}-{groups[2].zfill(2)}"
            elif len(groups) == 2:
                date_str = f"2026-{groups[0].zfill(2)}-{groups[1].zfill(2)}"
            elif len(groups) == 3 and len(groups[0]) == 2:
                date_str = f"20{groups[0]}-{groups[1].zfill(2)}-{groups[2].zfill(2)}"
            break
    result['date'] = date_str
    
    # 수량 추출
    qty_pattern = r'(\d{1,3}(?:,\d{3})*|\d+)\s*EA'
    quantities = re.findall(qty_pattern, text, re.IGNORECASE)
    result['quantity'] = '+'.join(quantities) + 'EA' if quantities else '-'
    
    # 브랜드/건명 추출
    brand_patterns = [
        r'#\s*([^\n*]+?)\s*(?:출고|픽업|납품|요청)',
        r'\*#([^\n*]+?)\*',
    ]
    brand = '-'
    for pattern in brand_patterns:
        match = re.search(pattern, text)
        if match:
            brand = match.group(1).strip()
            break
    result['brand'] = brand
    
    # 도착시간 추출
    time_pattern = r'(?:도착\s*시간|도착시간|도착)[^\d]*(\d{1,2}시(?:\s*\d{1,2}분)?)|오전\s*(\d{1,2}시)|오후\s*(\d{1,2}시)'
    time_match = re.search(time_pattern, text)
    if time_match:
        result['arrival_time'] = next(t for t in time_match.groups() if t)
    else:
        result['arrival_time'] = '-'
    
    return result

def get_weekday(date_str):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        weekdays = ["(월)", "(화)", "(수)", "(목)", "(금)", "(토)", "(일)"]
        return weekdays[dt.weekday()]
    except:
        return ""

def update_canvas(parsed):
    date = parsed.get('date')
    if not date:
        return
    
    weekday = get_weekday(date)
    section_title = f"📦 {date} {weekday}"
    new_row = f"|{parsed['brand']}|{parsed['quantity']}|{parsed['arrival_time']}|{parsed['order_number']}|"
    
    try:
        # 기존 Canvas 내용 읽기
        canvas = slack_client.canvases_sections_lookup(
            canvas_id=CANVAS_ID,
            criteria={"contains_text": section_title}
        )
        sections = canvas.get('sections', [])
        
        if sections:
            # 해당 날짜 섹션이 있으면 행 추가
            section_id = sections[0]['id']
            slack_client.canvases_sections_update(
                canvas_id=CANVAS_ID,
                section_id=section_id,
                action="append",
                content=new_row
            )
        else:
            # 해당 날짜 섹션이 없으면 새로 추가
            new_section = f"\n## {section_title}\n\n| 브랜드 / 건명 | 수량 | 도착시간 | 오더번호 |\n|---|---|---|---|\n{new_row}\n"
            slack_client.canvases_sections_update(
                canvas_id=CANVAS_ID,
                action="append",
                content=new_section
            )
    except Exception as e:
        print(f"Canvas 업데이트 오류: {e}")

@app.route("/slack/events", methods=["POST"])
def slack_events():
    data = request.json
    
    if "challenge" in data:
        return data["challenge"]
    
    event = data.get("event", {})
    event_id = data.get("event_id", "")
    
    if event_id in processed_events:
        return "OK"
    processed_events.add(event_id)
    
    if event.get("type") == "message" and not event.get("subtype"):
        channel = event.get("channel")
        text = event.get("text", "")
        
        if channel == CHANNEL_ID and "출고" in text:
            parsed = parse_message(text)
            if parsed.get('date'):
                update_canvas(parsed)
    
    return "OK"

if __name__ == "__name__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
