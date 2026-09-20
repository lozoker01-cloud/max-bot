import os
import json
import requests
import urllib3
import traceback
import re
from fastapi import FastAPI, Request, BackgroundTasks

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Жесткая очистка ключей
raw_groq_key = os.getenv("GROQ_API_KEY", "")
GROQ_API_KEY = raw_groq_key.replace("\n", "").replace("\r", "").strip()

raw_max_token = os.getenv("MAX_BOT_TOKEN", "")
MAX_BOT_TOKEN = raw_max_token.replace("\n", "").replace("\r", "").strip()

MAX_API_BASE = "https://platform-api2.max.ru"

# ВСТАВЬТЕ СЮДА ВАШ ВНУТРЕННИЙ ID
OPERATOR_ID = "20195632" 

# ВСТАВЬТЕ СЮДА АКТУАЛЬНУЮ ССЫЛКУ ИЗ GOOGLE APPS SCRIPT
GOOGLE_SHEET_WEBHOOK = "https://script.google.com/macros/s/AKfycbxnWcBZKEyrvlZ0FeiHJobR_DcsU_q5QHjnyH9ImfJ8p76RBeLfkR7CoKxqgF0Dqmpvhg/exec"

app = FastAPI()

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
                faq_items.append({"question": "Официальные правила", "answer": p})
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
            
    print(f"База знаний успешно собрана! Всего блоков: {len(faq_items)}")

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
    except:
        pass

@app.on_event("startup")
def startup_event():
    load_knowledge_base()
    register_webhook()

def clean_text(text: str) -> str:
    pattern = r"\[" + "c" + "ite:" + r"\s*\d+\]"
    return re.sub(pattern, "", text).strip()

def find_top_matches(user_message: str, top_n: int = 2) -> str:
    global faq_items
    if not faq_items:
        return ""

    # 1. Выделяем ключевые слова (как при поиске Ctrl+F)
    clean_msg = re.sub(r'[^\w\s]', ' ', user_message.lower())
    words = clean_msg.split()
    stop_words = {"что", "такое", "как", "где", "когда", "есть", "ли", "это", "кто", "могу", "мне", "меня", "какие", "у", "в", "на", "с", "по", "для", "можно", "а", "и", "или"}
    keywords = [w for w in words if len(w) > 2 and w not in stop_words]
    
    if not keywords:
        return ""

    scored_items = []
    for item in faq_items:
        if not isinstance(item, dict):
            continue
        
        q_text = clean_text(str(item.get('question', ''))).lower()
        a_text = clean_text(str(item.get('answer', ''))).lower()
        full_text_lower = q_text + " " + a_text
        
        score = 0
        for kw in keywords:
            # Берем корень слова для надежности поиска
            stem = kw[:-2] if len(kw) >= 5 else kw
            if stem in full_text_lower:
                score += 1
        
        # Полное совпадение фразы дает бонус
        if clean_msg in full_text_lower:
            score += 10
            
        if score > 0:
            scored_items.append((score, item))

    scored_items.sort(key=lambda x: x[0], reverse=True)
    
    # 2. Формируем контекст с ЖЕСТКОЙ ЗАЩИТОЙ ОТ ОШИБКИ 413 (Payload Too Large)
    top_matches = []
    current_length = 0
    MAX_CHARS = 3500 # Жесткий лимит символов (~1000 токенов, что гарантированно меньше лимита в 8000)

    for score, item in scored_items[:top_n]:
        q = clean_text(str(item.get('question', '')))
        a = clean_text(str(item.get('answer', '')))
        match_text = f"ФАКТ: {q}\nДЕТАЛИ: {a}\n\n"
        
        # Отсекаем лишний текст, если он превышает безопасный размер
        if current_length + len(match_text) > MAX_CHARS:
            allowed = MAX_CHARS - current_length
            if allowed > 100:
                top_matches.append(match_text[:allowed] + "... [ТЕКСТ ОБРЕЗАН]")
            break
            
        top_matches.append(match_text)
        current_length += len(match_text)

    return "".join(top_matches)

def get_groq_answer(user_message: str, is_first_message: bool) -> str:
    # Ищем текст по принципу Ctrl+F
    retrieved_faq = find_top_matches(user_message, top_n=2)
    
    if not retrieved_faq:
        return "К сожалению, у меня нет точной информации по данному вопросу."
    
    if is_first_message:
        greeting_rule = "1. БУДЬ ЧЕЛОВЕКОМ: Начни ответ вежливо (например, «Здравствуйте!», «Добрый день!»)."
    else:
        greeting_rule = "1. БУДЬ ЧЕЛОВЕКОМ: Отвечай сразу по делу. НЕ ЗДОРОВАЙСЯ, так как диалог уже идет."

    system_prompt = (
        "Ты — дружелюбный и компетентный консультант приемной комиссии РГСУ.\n"
        "ПРАВИЛА ОТВЕТА (ОЧЕНЬ ВАЖНО):\n"
        f"{greeting_rule}\n"
        "2. Отвечай на вопрос, используя ТОЛЬКО предоставленные факты из Базы Знаний.\n"
        "3. Перефразируй факты так, чтобы текст звучал красиво и понятно для абитуриента.\n\n"
        f"=== БАЗА ЗНАНИЙ (CTRL+F) ===\n{retrieved_faq}"
    )
      
    if not GROQ_API_KEY:
        return "🚨 ОШИБКА: Не задан GROQ_API_KEY в переменных окружения Render."

    url = "https://api.groq.com/openai/v1/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "openai/gpt-oss-120b",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        "temperature": 0.1,
        "max_tokens": 500
    }
    
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=15)
        data = res.json()
        if res.status_code == 200:
            return data["choices"][0]["message"]["content"]
        else:
            err_msg = data.get("error", {}).get("message", str(data))
            return f"🚨 ОШИБКА GROQ ({res.status_code}): {err_msg}"
    except Exception as e:
        return f"🚨 ОШИБКА ЗАПРОСА: {str(e)}"

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

    bot_reply = get_groq_answer(text, is_first_msg)
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
    return {"status": "Bot is running with Strict Ctrl+F Search & Groq Limit Protection!"}

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
