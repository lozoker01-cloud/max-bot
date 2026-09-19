import os
import json
import requests
import urllib3
import traceback
from fastapi import FastAPI, Request, BackgroundTasks
from groq import Groq

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
MAX_API_BASE = "https://platform-api2.max.ru"
OPERATOR_PHONE = "+79637862725" # Ваш номер для уведомлений

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
        print(f"Ошибка при регистрации вебхука: {e}")

@app.on_event("startup")
def startup_event():
    register_webhook()

def find_top_matches(user_message: str, top_n: int = 4) -> str:
    global faq_items
    if not faq_items:
        return "База FAQ пуста."

    user_words = set(user_message.lower().split())
    user_words = {w for w in user_words if len(w) > 2}
    
    if not user_words:
        res = []
        for item in faq_items[:top_n]:
            res.append(f"Вопрос: {item.get('question', '')}\nОтвет: {item.get('answer', '')}")
        return "\n\n".join(res)

    scored_items = []
    for item in faq_items:
        if not isinstance(item, dict):
            continue
        q_text = item.get('question', '')
        a_text = item.get('answer', '')
        
        item_words = set(q_text.lower().split()).union(set(a_text.lower().split()))
        score = len(user_words.intersection(item_words))
        scored_items.append((score, item))

    scored_items.sort(key=lambda x: x[0], reverse=True)
    
    top_matches = []
    for score, item in scored_items[:top_n]:
        top_matches.append(f"Вопрос: {item.get('question', '')}\nОтвет: {item.get('answer', '')}")

    return "\n\n".join(top_matches)

def get_groq_answer(user_message: str) -> str:
    retrieved_faq = find_top_matches(user_message, top_n=4)
    
    system_prompt = (
        "Ты — официальный информационный бот приемной комиссии РГСУ для соцсетей.\n"
        "Отвечай ТОЛЬКО на основе предоставленных ниже Частых Вопросов (FAQ).\n\n"
        "ТВОИ ПРАВИЛА ОФОРМЛЕНИЯ ТЕКСТА (ЭТО КРИТИЧЕСКИ ВАЖНО):\n"
        "1. ОБЯЗАТЕЛЬНО ставь точку в конце последнего предложения. Ни один твой ответ не должен обрываться без точки.\n"
        "2. ОБЯЗАТЕЛЬНО дели текст на смысловые абзацы. Между абзацами должна быть пустая строка. Никогда не пиши сплошной стеной текста!\n"
        "3. Если перечисляешь факты, используй списки через дефис (-).\n"
        "4. Ответ должен быть емким, вежливым и понятным (3-5 предложений).\n"
        "5. Если информации нет в FAQ, отвечай: «К сожалению, у меня нет точной информации по данному вопросу.»\n\n"
        f"=== ЧАСТЫЕ ВОПРОСЫ (FAQ) ===\n{retrieved_faq}"
    )
      
    response = groq_client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        model="openai/gpt-oss-120b",
        temperature=0.1, 
        max_tokens=300
    )
    return response.choices[0].message.content

def send_message_to_max(target_id: str, text: str, include_button: bool = False):
    if not MAX_BOT_TOKEN:
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
        
        # Добавляем кнопку вызова оператора, если это ответ бота
        if include_button:
            button_payload = [
                [{"text": "👤 Связаться с оператором", "callback_data": "operator_request"}]
            ]
            # МАКС поддерживает разные стандарты кнопок, отправляем оба варианта для надежности
            payload["reply_markup"] = {"inline_keyboard": button_payload}
            payload["keyboard"] = {"inline": True, "buttons": button_payload}

        try:
            res = requests.post(f"{MAX_API_BASE}/messages", headers=headers, params=params, json=payload, verify=False, timeout=5)
            if res.status_code == 200:
                break
        except Exception as e:
            print(f"Ошибка отправки: {e}")

# Фоновые задачи для предотвращения дублирования сообщений
def process_user_message(chat_id: str, text: str):
    if text.lower() in ["/start", "старт", "привет"]:
        reply = "Здравствуйте! Я цифровой ассистент приемной комиссии РГСУ. Задайте мне любой вопрос о поступлении, и я постараюсь помочь!"
        send_message_to_max(chat_id, reply, include_button=False)
        return
        
    bot_reply = get_groq_answer(text)
    send_message_to_max(chat_id, bot_reply, include_button=True)

def transfer_to_operator(user_id: str):
    # Пишем пользователю, что переводим его
    send_message_to_max(user_id, "⏳ Переключаю вас на специалиста приемной комиссии. Пожалуйста, подождите, скоро вам ответят.", include_button=False)
    
    # Отправляем уведомление вам на телефон/в чат
    alert_text = f"🚨 ВНИМАНИЕ!\nПользователь не получил ответа от бота и запросил помощь оператора.\nID пользователя для связи: {user_id}"
    send_message_to_max(OPERATOR_PHONE, alert_text, include_button=False)


@app.get("/")
def root():
    return {"status": "RGSU Bot is running with Buttons and Background Tasks!"}

@app.api_route("/webhook", methods=["GET", "POST"])
async def max_webhook(request: Request, background_tasks: BackgroundTasks):
    if request.method == "GET":
        return {"status": "Webhook is active!"}
      
    try:
        body_bytes = await request.body()
        data = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        
        update_type = data.get("update_type", "")
        
        # ОБРАБОТКА НАЖАТИЯ НА КНОПКУ (Callback)
        if update_type == "message_callback" or "callback" in data:
            callback_info = data.get("callback", {})
            cb_data = callback_info.get("data") or data.get("callback_data")
            
            # Извлекаем ID того, кто нажал кнопку
            sender_id = callback_info.get("from", {}).get("user_id") or callback_info.get("from", {}).get("id") or ""
            
            if cb_data == "operator_request" and sender_id:
                # Запускаем перевод на оператора в фоне
                background_tasks.add_task(transfer_to_operator, str(sender_id))
            
            return {"status": "ok"}
          
        # ОБРАБОТКА ОБЫЧНОГО СООБЩЕНИЯ
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

        # Если есть текст и ID, ЗАПУСКАЕМ ОБРАБОТКУ В ФОНЕ (решает проблему дублирования)
        if message_text and chat_id:
            background_tasks.add_task(process_user_message, str(chat_id), message_text)
          
    except Exception as e:
        print(f"ОШИБКА В WEBHOOK: {type(e).__name__}: {e}")
        traceback.print_exc()
          
    # Мгновенно возвращаем ОК серверу МАКС, чтобы он не спамил дублями
    return {"status": "ok"}
