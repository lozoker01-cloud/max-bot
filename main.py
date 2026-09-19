import os
import json
import requests
import urllib3
import traceback
import re
from fastapi import FastAPI, Request, BackgroundTasks
from groq import Groq

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
MAX_API_BASE = "https://platform-api2.max.ru"

# ВСТАВЬТЕ СЮДА ВАШ ВНУТРЕННИЙ ID (который выдает команда /myid)
OPERATOR_ID = "20195632" 

app = FastAPI()
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

print("Загрузка базы знаний (FAQ)...")
try:
    with open('faq_data.json', 'r', encoding='utf-8') as f:
        faq_raw = json.load(f)
    print("FAQ успешно загружен!")
except Exception as e:
    print(f"Ошибка загрузки faq_data.json: {e}")
    faq_raw = {}

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

faq_items = flatten_faq(faq_raw)

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
    register_webhook()

def find_top_matches(user_message: str, top_n: int = 5) -> str:
    global faq_items
    if not faq_items:
        return "База FAQ пуста."

    clean_msg = re.sub(r'[^\w\s]', '', user_message.lower())
    user_words = set(clean_msg.split())
    
    stop_words = {"что", "такое", "как", "где", "когда", "есть", "ли", "это", "кто", "могу", "мне", "меня", "какие", "у", "в", "на", "с", "по"}
    user_words = {w for w in user_words if len(w) > 2 and w not in stop_words}
    
    if not user_words:
        return "\n\n".join([f"Вопрос: {i.get('question', '')}\nОтвет: {i.get('answer', '')}" for i in faq_items[:top_n]])

    scored_items = []
    for item in faq_items:
        if not isinstance(item, dict):
            continue
        q_text = item.get('question', '').lower()
        a_text = item.get('answer', '').lower()
        full_text = q_text + " " + a_text
        
        score = 0
        for w in user_words:
            # Умное обрезание окончаний (более безопасное)
            stem = w[:-2] if len(w) >= 6 else (w[:-1] if len(w) == 5 else w)
            if stem in full_text:
                score += 1
        
        if clean_msg in full_text:
            score += 10
            
        scored_items.append((score, item))

    scored_items.sort(key=lambda x: x[0], reverse=True)
    
    top_matches = []
    for score, item in scored_items[:top_n]:
        if score > 0:
            top_matches.append(f"Вопрос: {item.get('question', '')}\nОтвет: {item.get('answer', '')}")

    if not top_matches:
        return "Нет информации в базе."

    return "\n\n".join(top_matches)

def get_groq_answer(user_message: str) -> str:
    retrieved_faq = find_top_matches(user_message, top_n=5)
    
    system_prompt = (
        "Ты — официальный бот приемной комиссии РГСУ.\n"
        "Отвечай ТОЛЬКО на основе FAQ ниже.\n\n"
        "ПРАВИЛА:\n"
        "1. Будь умным: если спрашивают про 'документы', а в FAQ есть про 'подачу заявления' — это одно и то же. Отвечай смело.\n"
        "2. ОБЯЗАТЕЛЬНО ставь точку в конце.\n"
        "3. ОБЯЗАТЕЛЬНО дели текст на абзацы (пустая строка между ними).\n"
        "4. Если информации СОВСЕМ нет в FAQ, отвечай: «К сожалению, у меня нет точной информации по данному вопросу.»\n\n"
        f"=== ЧАСТЫЕ ВОПРОСЫ ===\n{retrieved_faq}"
    )
      
    try:
        response = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            model="openai/gpt-oss-120b",
            temperature=0.01, # Снизили до минимума для максимальной стабильности
            max_tokens=400
        )
        return response.choices[0].message.content
    except:
        return "Произошла ошибка при генерации ответа."

def send_message_to_max(target_id: str, text: str):
    if not MAX_BOT_TOKEN or not target_id:
        return
    
    headers = {
        "Authorization": f"{MAX_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    
    for id_param in ["user_id", "chat_id"]:
        params = {id_param: target_id}
        payload = {
            id_param: target_id,
            "text": text
        }

        try:
            res = requests.post(f"{MAX_API_BASE}/messages", headers=headers, params=params, json=payload, verify=False, timeout=5)
            if res.status_code == 200:
                break
        except Exception as e:
            pass

def transfer_to_operator(user_id: str):
    # 1. Пишем пользователю
    send_message_to_max(user_id, "⏳ Переключаю вас на специалиста приемной комиссии. Пожалуйста, подождите, скоро вам ответят.")
    
    # 2. Уведомляем администратора с ИНСТРУКЦИЕЙ
    if OPERATOR_ID:
        alert_text = (
            f"🚨 ВНИМАНИЕ!\nПользователь запросил помощь оператора.\n"
            f"Его ID: {user_id}\n\n"
            f"👇 Чтобы ответить ему, скопируйте команду ниже, через пробел напишите свой текст и отправьте боту:\n\n"
            f"/ответ {user_id} "
        )
        send_message_to_max(OPERATOR_ID, alert_text)

def process_user_message(chat_id: str, text: str):
    # 1. ФУНКЦИЯ ОТВЕТА ОТ ОПЕРАТОРА
    if str(chat_id) == str(OPERATOR_ID) and text.lower().startswith("/ответ"):
        parts = text.split(" ", 2)
        if len(parts) >= 3:
            target_user = parts[1]
            reply_text = parts[2]
            # Отправляем сообщение абитуриенту от лица оператора
            send_message_to_max(target_user, f"👨‍💻 *Сообщение от специалиста приемной комиссии:*\n\n{reply_text}")
            # Подтверждаем вам, что ушло
            send_message_to_max(chat_id, f"✅ Сообщение успешно отправлено пользователю {target_user}!")
        else:
            send_message_to_max(chat_id, "❌ Ошибка формата. Напишите так:\n/ответ [ID_пользователя] [ваш текст]")
        return

    # 2. КОМАНДА ДЛЯ ПОЛУЧЕНИЯ ID
    if text.strip().lower() == "/myid":
        send_message_to_max(chat_id, f"✅ Ваш внутренний ID:\n{chat_id}")
        return

    # ОБЫЧНЫЕ ВОПРОСЫ
    if text.lower() in ["/start", "старт", "привет"]:
        reply = "Здравствуйте! Я цифровой ассистент приемной комиссии РГСУ. Задайте мне любой вопрос о поступлении, и я постараюсь помочь!"
        send_message_to_max(chat_id, reply)
        return
        
    if "оператор" in text.lower():
        transfer_to_operator(chat_id)
        return
        
    bot_reply = get_groq_answer(text)
    
    if "к сожалению" not in bot_reply.lower():
        bot_reply += "\n\n---\n*Если я не смог полностью ответить на ваш вопрос, напишите слово «Оператор».*"
    else:
        bot_reply += "\n\n*Для связи со специалистом напишите слово «Оператор».*"
        
    send_message_to_max(chat_id, bot_reply)

@app.get("/")
def root():
    return {"status": "Bot is running. Reply feature active!"}

@app.api_route("/webhook", methods=["GET", "POST"])
async def max_webhook(request: Request, background_tasks: BackgroundTasks):
    if request.method == "GET":
        return {"status": "Webhook is active!"}
      
    try:
        body_bytes = await request.body()
        data = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        
        msg_block = data.get("message", {})
        message_text = (
            msg_block.get("body", {}).get("text") or 
            msg_block.get("text") or 
            data.get("text") or 
            ""
        )
          
        chat_id = (
            msg_block.get("chat_id") or 
            msg_block.get("sender", {}).get("user_id") or 
            msg_block.get("sender", {}).get("id") or 
            msg_block.get("recipient", {}).get("chat_id") or
            data.get("chat_id") or 
            data.get("user_id") or 
            ""
        )
          
        if not chat_id and isinstance(msg_block, dict):
            for k, v in msg_block.items():
                if isinstance(v, dict):
                    if "id" in v:
                        chat_id = v["id"]
                        break
                    if "user_id" in v:
                        chat_id = v["user_id"]
                        break

        if message_text and chat_id:
            background_tasks.add_task(process_user_message, str(chat_id), message_text)
          
    except Exception as e:
        traceback.print_exc()
          
    return {"status": "ok"}
