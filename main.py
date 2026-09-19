import os
import json
import requests
import urllib3
import traceback
import re
from fastapi import FastAPI, Request, BackgroundTasks
from openai import OpenAI

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
MAX_API_BASE = "https://platform-api2.max.ru"

# ВСТАВЬТЕ СЮДА ВАШ ВНУТРЕННИЙ ID
OPERATOR_ID = "20195632" 

# ВСТАВЬТЕ СЮДА АКТУАЛЬНУЮ ССЫЛКУ ИЗ GOOGLE APPS SCRIPT (БЕЗ СЛЕША НА КОНЦЕ)
GOOGLE_SHEET_WEBHOOK = "https://script.google.com/macros/s/AKfycbvH9-hsOZo7ADggN9jfME4cTgqL1r5bGAJXrtWor-iObdFFlJ3L3Cs6tWqS0X-6b8xQ/exec"

app = FastAPI()
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

user_history = {}
faq_items = []

def flatten_faq(data):
    flat_list = []
    if isinstance(data, dict):
        if "categories" in data:
            for cat in data["categories"]:
                if isinstance(cat, dict) and "items" in cat:
                    for item in cat["items"]:
                        if isinstance(item, dict):
                            flat_list.append(item)
        elif "question" in data:
            flat_list.append(data)
        else:
            for v in data.values():
                if isinstance(v, list):
                    for sub in v:
                        if isinstance(sub, dict):
                            flat_list.append(sub)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                if "items" in item:
                    flat_list.extend(item["items"])
                elif "question" in item:
                    flat_list.append(item)
    return flat_list

def load_knowledge_base():
    """Собирает знания из 3 источников"""
    global faq_items
    faq_items = []
    
    print("Загрузка 1/3: faq_data.json...")
    try:
        with open('faq_data.json', 'r', encoding='utf-8') as f:
            faq_raw = json.load(f)
            faq_items.extend(flatten_faq(faq_raw))
    except Exception as e:
        print(f"Ошибка JSON: {e}")

    print("Загрузка 2/3: pravila.txt...")
    try:
        with open('pravila.txt', 'r', encoding='utf-8') as f:
            content = f.read()
            paragraphs = [p.strip() for p in content.split('\n\n') if len(p.strip()) > 20]
            for p in paragraphs:
                faq_items.append({"question": "Официальные правила приема РГСУ", "answer": p})
    except Exception as e:
        print(f"Ошибка TXT: {e}")

    print("Загрузка 3/3: Google Таблицы (Динамическая база)...")
    if GOOGLE_SHEET_WEBHOOK:
        try:
            res = requests.get(GOOGLE_SHEET_WEBHOOK, timeout=10)
            if res.status_code == 200:
                dynamic_data = res.json()
                for item in dynamic_data:
                    faq_items.append({"question": item.get("question", ""), "answer": item.get("answer", "")})
        except Exception as e:
            print(f"Ошибка связи с Таблицей: {e}")
            
    print(f"База знаний успешно собрана! Всего правил: {len(faq_items)}")

def register_webhook():
    if not MAX_BOT_TOKEN:
        return
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if not render_url:
        return
      
    webhook_url = f"{render_url}/webhook"
    headers = {
        "Authorization": f"{MAX_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "url": webhook_url,
        "update_types": ["message_created", "message_callback"]
    }
    try:
        requests.post(f"{MAX_API_BASE}/subscriptions", headers=headers, json=payload, verify=False, timeout=10)
    except Exception as e:
        pass

@app.on_event("startup")
def startup_event():
    load_knowledge_base()
    register_webhook()

def clean_text(text: str) -> str:
    pattern = r"\[" + "c" + "ite:" + r"\s*\d+\]"
    return re.sub(pattern, "", text).strip()

def find_top_matches(user_message: str, top_n: int = 4) -> str:
    global faq_items
    if not faq_items:
        return "База пуста."

    clean_msg = re.sub(r'[^\w\s]', ' ', user_message.lower())
    clean_msg = re.sub(r'\s+', ' ', clean_msg).strip()
    
    user_words = set(clean_msg.split())
    stop_words = {"что", "такое", "как", "где", "когда", "есть", "ли", "это", "кто", "могу", "мне", "меня", "какие", "у", "в", "на", "с", "по", "для", "можно"}
    user_words = {w for w in user_words if len(w) > 2 and w not in stop_words}
    
    if not user_words:
        return "Недостаточно ключевых слов для поиска."

    scored_items = []
    for item in faq_items:
        if not isinstance(item, dict):
            continue
        
        q_text = clean_text(item.get('question', '')).lower()
        a_text = clean_text(item.get('answer', '')).lower()
        full_text = q_text + " " + a_text
        
        score = 0
        for w in user_words:
            stem = w[:-2] if len(w) >= 6 else (w[:-1] if len(w) == 5 else w)
            if stem in full_text:
                score += 1
        
        if clean_msg in re.sub(r'[^\w\s]', ' ', full_text):
            score += 10
            
        scored_items.append((score, item))

    scored_items.sort(key=lambda x: x[0], reverse=True)
    
    top_matches = []
    for score, item in scored_items[:top_n]:
        if score > 0:
            q = clean_text(item.get('question', ''))
            a = clean_text(item.get('answer', ''))
            top_matches.append(f"Контекст: {q}\nТекст: {a}")

    if not top_matches:
        return "Нет информации в базе."

    return "\n\n".join(top_matches)

def get_gpt_answer(user_message: str, is_first_message: bool) -> str:
    retrieved_faq = find_top_matches(user_message, top_n=4)
    
    if is_first_message:
        greeting_rule = "1. БУДЬ ЧЕЛОВЕКОМ: Начни ответ вежливо (например, «Здравствуйте!», «Добрый день!»)."
    else:
        greeting_rule = "1. БУДЬ ЧЕЛОВЕКОМ: Отвечай сразу по делу. НЕ ЗДОРОВАЙСЯ, так как диалог уже идет."

    system_prompt = (
        "Ты — дружелюбный, официальный и компетентный консультант приемной комиссии РГСУ.\n"
        "Твоя задача — отвечать на вопросы абитуриентов, используя ТОЛЬКО предоставленную Базу Знаний.\n\n"
        "ПРАВИЛА ОТВЕТА (ОЧЕНЬ ВАЖНО):\n"
        f"{greeting_rule} Формулируй ответ полными, красивыми предложениями.\n"
        "2. ОПИРАЙСЯ ТОЛЬКО НА БАЗУ: Вся информация в твоем ответе должна быть взята ИЗ СТРОК НИЖЕ.\n"
        "3. ОТСУТСТВИЕ ИНФОРМАЦИИ: Если в Базе Знаний нет ответа, отвечай ТОЧНО этой фразой: «К сожалению, у меня нет точной информации по данному вопросу.»\n\n"
        f"=== БАЗА ЗНАНИЙ ===\n{retrieved_faq}"
    )
      
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.2, 
            max_tokens=600
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"🚨 ОШИБКА GPT: {str(e)}"

def send_message_to_max(target_id: str, text: str):
    if not MAX_BOT_TOKEN or not target_id:
        return
    headers = {"Authorization": f"{MAX_BOT_TOKEN}", "Content-Type": "application/json"}
    for id_param in ["user_id", "chat_id"]:
        try:
            res = requests.post(f"{MAX_API_BASE}/messages", headers=headers, params={id_param: target_id}, json={id_param: target_id, "text": text}, verify=False, timeout=5)
            if res.status_code == 200:
                break
        except:
            pass

def log_to_google_sheet(user_name, user_id, question, answer):
    if not GOOGLE_SHEET_WEBHOOK:
        return
    payload = {"name": user_name, "id": user_id, "question": question, "answer": answer}
    try:
        requests.post(GOOGLE_SHEET_WEBHOOK, json=payload, timeout=5)
    except:
        pass

def transfer_to_operator(user_id: str, user_name: str):
    send_message_to_max(user_id, "⏳ Переключаю вас на специалиста приемной комиссии. Пожалуйста, подождите, скоро вам ответят.")
    history = user_history.get(user_id, {})
    last_q = history.get("question", "Неизвестно")
    last_a = history.get("answer", "Нет ответа")
    if OPERATOR_ID:
        alert_text = (f"🚨 ЗАПРОС ОПЕРАТОРА\n\n👤 Имя: {user_name}\n🆔 ID: {user_id}\n\n❓ Вопрос:\n{last_q}\n\n🤖 Ответ бота:\n{last_a}\n\n👇 Для ответа скопируйте:\n\n/ответ {user_id} ")
        send_message_to_max(OPERATOR_ID, alert_text)

def process_user_message(chat_id: str, text: str, user_name: str):
    if str(chat_id) == str(OPERATOR_ID) and text.lower().startswith("/ответ"):
        parts = text.split(" ", 2)
        if len(parts) >= 3:
            send_message_to_max(parts[1], f"👨‍💻 *Сообщение от специалиста приемной комиссии:*\n\n{parts[2]}")
            send_message_to_max(chat_id, f"✅ Ответ отправлен пользователю {parts[1]}!")
        else:
            send_message_to_max(chat_id, "❌ Ошибка формата. Напишите так:\n/ответ [ID] [текст]")
        return

    if text.strip().lower() == "/myid":
        send_message_to_max(chat_id, f"✅ Ваш внутренний ID:\n{chat_id}")
        return

    if text.lower() in ["/start", "старт", "привет"]:
        send_message_to_max(chat_id, "Здравствуйте! Я цифровой ассистент приемной комиссии РГСУ. Задайте мне любой вопрос о поступлении, и я постараюсь помочь!")
        return
        
    if "оператор" in text.lower():
        transfer_to_operator(chat_id, user_name)
        return
        
    if chat_id not in user_history:
        user_history[chat_id] = {"count": 0, "question": "", "answer": ""}
    
    user_history[chat_id]["count"] += 1
    is_first_msg = (user_history[chat_id]["count"] == 1)

    bot_reply = get_gpt_answer(text, is_first_msg)
    user_history[chat_id]["question"] = text
    user_history[chat_id]["answer"] = bot_reply
    
    if "🚨 ОШИБКА" not in bot_reply:
        if "к сожалению" not in bot_reply.lower():
            final_reply = bot_reply + "\n\n---\n*Если я не смог полностью ответить на ваш вопрос, напишите слово «Оператор».*"
        else:
            final_reply = bot_reply + "\n\n*Для связи со специалистом напишите слово «Оператор».*"
    else:
        final_reply = bot_reply

    send_message_to_max(chat_id, final_reply)
    log_to_google_sheet(user_name, chat_id, text, bot_reply)

@app.get("/")
def root():
    return {"status": "Bot is running with GPT-4o-mini & Tri-Layer Memory!"}

@app.get("/reload_faq")
def api_reload_faq():
    load_knowledge_base()
    return {"status": "success", "rules_loaded": len(faq_items)}

@app.api_route("/webhook", methods=["GET", "POST"])
async def max_webhook(request: Request, background_tasks: BackgroundTasks):
    if request.method == "GET":
        return {"status": "Webhook is active!"}
      
    try:
        body_bytes = await request.body()
        data = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        msg_block = data.get("message", {})
        message_text = (msg_block.get("body", {}).get("text") or msg_block.get("text") or data.get("text") or "")
        sender_info = msg_block.get("sender", {})
        user_name = sender_info.get("name") or sender_info.get("username") or "Абитуриент"
        chat_id = (msg_block.get("chat_id") or sender_info.get("user_id") or sender_info.get("id") or msg_block.get("recipient", {}).get("chat_id") or data.get("chat_id") or data.get("user_id") or "")
          
        if not chat_id and isinstance(msg_block, dict):
            for k, v in msg_block.items():
                if isinstance(v, dict) and "id" in v:
                    chat_id = v["id"]
                    break

        if message_text and chat_id:
            background_tasks.add_task(process_user_message, str(chat_id), message_text, str(user_name))
    except Exception as e:
        traceback.print_exc()
    return {"status": "ok"}
