import os
import re
import json
from flask import Flask, request
from slack_sdk import WebClient
import anthropic
from datetime import datetime

app = Flask(__name__)

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
CANVAS_ID = os.environ.get("CANVAS_ID")
CHANNEL_ID = os.environ.get("CHANNEL_ID")

slack_client = WebClient(token=SLACK_TOKEN)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

processed_events = set()

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
        
        if channel == CHANNEL_ID and ("출고" in text or "픽업" in text or "도착" in text):
            update_canvas(text)
    
    return "OK"

def update_canvas(message_text):
    current_canvas = slack_client.canvases_sections_lookup(
        canvas_id=CANVAS_ID,
        criteria={"contains_text": "출고"}
    )
    
    prompt = f"""
다음 Slack 출고 요청 메시지를 분석해서 아래 JSON 형식으로만 응답해줘. 다른 텍스트 없이 JSON만.

메시지:
{message_text}

JSON 형식:
{{
  "date": "YYYY-MM-DD",
  "brand": "브랜드/건명",
  "quantity": "수량",
  "arrival_time": "도착시간",
  "order_number": "오더번호"
}}

날짜를 찾을 수 없으면 date를 null로.
"""
    
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    
    try:
        parsed = json.loads(response.content[0].text)
        if parsed.get("date"):
            date = parsed["date"]
            dt = datetime.strptime(date, "%Y-%m-%d")
            weekdays = ["(월)", "(화)", "(수)", "(목)", "(금)", "(토)", "(일)"]
            weekday = weekdays[dt.weekday()]
            
            section_title = f"📦 {date} {weekday}"
            new_row = f"|{parsed['brand']}|{parsed['quantity']}|{parsed['arrival_time']}|{parsed['order_number']}|"
            
            slack_client.canvases_sections_lookup(canvas_id=CANVAS_ID)
            slack_client.api_call(
                "canvases.sections.update",
                json={
                    "canvas_id": CANVAS_ID,
                    "section_id": section_title,
                    "content": new_row
                }
            )
    except:
        pass

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
