import os
import re
import json
from flask import Flask, request, jsonify
from slack_sdk import WebClient
from datetime import datetime, timezone, timedelta
import threading
import time
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
CALENDAR_CHANNEL_ID = "C0B92726KKM"
ACCIDENT_CHANNEL_ID = "C0AGWG4QALV"  # 4-사고접수 (이름 바뀌어도 ID는 유지됨)

slack_client = WebClient(token=SLACK_TOKEN)
processed_events = set()

# ==========================================================
# [신규] 사고접수 → Google Sheets 자동 기록
# 시트 열: A요청일 B주문번호 C송장번호 D타이틀/요청내용 E완료여부
#          F담당자 G완료일 H비고 I메시지ID
# ==========================================================
KST = timezone(timedelta(hours=9))

MANAGERS = {
    "U0AHCLVUW3T": "심햇님",
    "U0B4CRHTSJZ": "오태완",
}

# ----- 스레드 댓글 상태 판정 키워드 (반드시 '띄어쓰기 없이' 작성할 것) -----
# 댓글 텍스트에서 공백을 모두 제거한 뒤 비교하므로,
# "확인 완료" / "확인완료" / "확인  완료" 모두 동일하게 잡힙니다.
DONE_WORDS = ("확인완료", "확인되었", "확인됐", "확인했습니다", "확인하였습니다")
CHECKING_WORDS = ("확인후답변", "확인후회신")
# 질문형 오탐 방지 ("확인되었는지 알려주세요" 같은 댓글이 완료로 잡히지 않도록)
EXCLUDE_WORDS = ("확인되었는지", "확인됐는지", "확인되었나")


def get_worksheet():
    creds_info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    return sh.worksheet(os.environ.get("SHEET_NAME", "사고접수"))


def normalize(text):
    """별표(굵게), 목록기호(1. •) 등 슬랙 서식 제거"""
    text = text.replace("*", "")
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"^\s*(?:\d+[.)]|[•◦▪-])\s*", "", line)
        lines.append(line)
    return "\n".join(lines)


def extract_field(text, field_name):
    """'주문번호: xxx' / '송장번호: xxx' / '요청내용: xxx' 형태에서 값 추출"""
    m = re.search(rf"{field_name}\s*[:：]\s*([^\n]*)", text)
    if not m:
        return ""
    value = m.group(1).strip()
    if "번호" in field_name:
        value = re.sub(r"[^A-Za-z0-9\-]", "", value)
    return value


def handle_accident_message(event):
    """새 접수 글 → 시트에 행 추가 (열 밀림 방지: 항상 9열 고정)"""
    raw = event.get("text", "")
    text = normalize(clean(raw))
    ts = event.get("ts")

    flat = re.sub(r"\s+", " ", text).strip()
    if not flat and not event.get("files"):
        return

    order_no = extract_field(text, "주문번호") or ""
    invoice_no = extract_field(text, "송장번호") or ""
    req_content = extract_field(text, "요청내용") or ""

    if req_content:
        title_m = re.search(r"#\s*([^\n]+)", text)
        if title_m:
            req_content = f"[{title_m.group(1).strip()}] {req_content}"
    else:
        req_content = flat

    if event.get("files"):
        req_content = (req_content + " [첨부있음]").strip() if req_content else "(파일/이미지 첨부)"

    req_date = datetime.fromtimestamp(float(ts), KST).strftime("%Y-%m-%d")

    # A요청일 B주문번호 C송장번호 D내용 E완료여부 F담당자 G완료일 H비고 I메시지ID
    row = [
        str(req_date),
        str(order_no),
        str(invoice_no),
        str(req_content)[:500],
        "미진행",
        "심햇님,오태완",
        "",
        "",
        "'" + str(ts),
    ]
    try:
        ws = get_worksheet()
        ws.append_row(row, value_input_option="USER_ENTERED",
                      table_range="A1")
        print(f"시트 행 추가 완료: [{order_no}] [{invoice_no}] {req_content[:30]}")
    except Exception as e:
        print(f"시트 행 추가 오류: {e}")


def handle_accident_reply(event):
    """스레드 댓글 → 담당자/완료여부/완료일 업데이트"""
    thread_ts = event.get("thread_ts")
    text = clean(event.get("text", ""))
    user = event.get("user", "")
    reply_ts = event.get("ts")

    try:
        ws = get_worksheet()
        id_column = ws.col_values(9)  # I열(메시지ID)

        target = str(thread_ts).split(".")[0]
        row = None
        for i, val in enumerate(id_column):
            v = str(val).replace("'", "").strip()
            if v.split(".")[0] == target:
                row = i + 1
                break

        if not row:
            print(f"원글을 시트에서 못 찾음: {thread_ts}")
            return

        # 담당자 자동 지정 (F열=6)
        if user in MANAGERS:
            ws.update_cell(row, 6, MANAGERS[user])

        # 완료여부(E열=5)/완료일(G열=7)
        norm = text.replace(" ", "")

        is_done = any(w in norm for w in DONE_WORDS) and not any(w in norm for w in EXCLUDE_WORDS)
        is_checking = any(w in norm for w in CHECKING_WORDS)

        if is_done:
            done_date = datetime.fromtimestamp(float(reply_ts), KST).strftime("%Y-%m-%d")
            ws.update_cell(row, 5, "완료")
            ws.update_cell(row, 7, done_date)
            print(f"{row}행 완료 처리")
        elif is_checking:
            current = ws.cell(row, 5).value
            if current != "완료":
                ws.update_cell(row, 5, "확인중")
                print(f"{row}행 확인중 처리")
    except Exception as e:
        print(f"시트 업데이트 오류: {e}")


# ==========================================================
# [기존] 출고오더 캘린더 봇
# ==========================================================
def clean(text):
    text = re.sub(r'<@[A-Z0-9]+\|[^>]+>', '', text)
    text = re.sub(r'<@[A-Z0-9]+>', '', text)
    text = re.sub(r'<!subteam\^[^>]+>', '', text)
    text = re.sub(r'<tel:[^>|]+\|([^>]+)>', r'\1', text)
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

def extract_driver_from_reply(text):
    """'차량정보 전달드립니다' 다음 3줄(이름/연락처/차량번호) 추출"""
    lines = [l.strip() for l in text.split('\n')]
    for i, line in enumerate(lines):
        if '차량정보' in line:
            info = []
            for nxt in lines[i+1:]:
                if nxt:
                    info.append(nxt)
                if len(info) >= 3:
                    break
            if info:
                return ' / '.join(info)
    return None

def parse_message(text):
    result = {}
    clean_text = clean(text)

    order_pattern = r'26\d{4}_[A-Za-z가-힣]+_[A-Za-z가-힣0-9()]+(?:\([^)]+\))?'
    orders = re.findall(order_pattern, clean_text)
    result['order_number'] = ' / '.join(orders) if orders else '-'

    result['date'] = extract_date(clean_text)
    result['brand'] = extract_brand(clean_text)
    result['driver'] = '-'

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
    print("정렬 시작!")
    try:
        result = slack_client.conversations_history(channel=CHANNEL_ID, limit=200)
        messages = result.get("messages", [])

        parsed_list = []
        for msg in messages:
            text = msg.get("text", "")
            if "출고" not in text:
                continue
            parsed = parse_message(text)
            if not parsed.get("date"):
                continue

            if msg.get("reply_count", 0) > 0:
                try:
                    replies = slack_client.conversations_replies(
                        channel=CHANNEL_ID, ts=msg["ts"], limit=50
                    )
                    for reply in replies.get("messages", []):
                        if reply.get("ts") == msg["ts"]:
                            continue
                        r_text = clean(reply.get("text", ""))
                        driver = extract_driver_from_reply(r_text)
                        if driver:
                            parsed['driver'] = driver
                            break
                except Exception as e:
                    print(f"스레드 읽기 오류: {e}")

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
            lines.append(f"{'브랜드/건명':<20} {'도착시간':<10} {'오더번호':<26} {'기사정보'}")
            lines.append("-" * 95)
            for item in items:
                lines.append(f"{item['brand']:<20} {item['arrival_time']:<10} {item['order_number']:<26} {item.get('driver','-')}")
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

# ==========================================================
# Slack Events (두 채널 공용)
# ==========================================================
ALLOWED_SUBTYPES = (None, "thread_broadcast", "file_share")

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

    if event.get("type") == "message" and event.get("subtype") in ALLOWED_SUBTYPES and not event.get("bot_id"):
        channel = event.get("channel")
        text = event.get("text", "")
        print(f"메시지 수신: channel={channel}, text={text[:80]}")

        # [기존] 출고오더 → 캘린더 채널
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

        # [신규] 사고접수 채널
        if channel == ACCIDENT_CHANNEL_ID:
            if event.get("thread_ts") and event.get("thread_ts") != event.get("ts"):
                handle_accident_reply(event)    # 스레드 댓글 → 상태 업데이트
            else:
                handle_accident_message(event)  # 새 접수 글 → 행 추가

    return "OK"

@app.route("/trigger-sort", methods=["GET"])
def trigger_sort():
    t = threading.Thread(target=collect_and_sort, daemon=True)
    t.start()
    return "정렬 시작! 1~2분 후 캘린더 채널을 확인해주세요."

@app.route("/ping", methods=["GET"])
def ping():
    return "alive"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
