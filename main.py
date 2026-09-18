import os
import json
import requests
import urllib3
import traceback
from fastapi import FastAPI, Request
from groq import Groq

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
MAX_API_BASE = "https://platform-api2.max.ru"

app = FastAPI()
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

print("Загрузка базы знаний...")
try:
    with open('faq_data.json', 'r', encoding='utf-8') as f:
        faq_raw = json.load(f)
    print("База знаний успешно загружена!")
except Exception as e:
    print(f"Ошибка загрузки faq_data.json: {e}")
    faq_raw = {}

# Функция для превращения вложенных категорий в плоский список вопросов
def flatten_faq(data):
    flat_list = []
    if isinstance(data, dict):
        # Если это структура с ключом "categories"
        if "categories" in data:
            for cat in data["categories"]:
                if isinstance(cat, dict) and "items" in cat:
                    for item in cat["items"]:
                        if isinstance(item, dict):
                            flat_list.append(item)
        # Если это просто словарь с вопросами
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

def find_best_match(user_message: str) -> str:
    global faq_items
    if not faq_items:
        return "Вопрос: Консультация\nОтвет: Обратитесь в приемную комиссию РГСУ по телефону +7-495-255-67-67."

    user_words = set(user_message.lower().split())
    user_words = {w for w in user_words if len(w) > 2}
    
    first_item = faq_items[0]

    if not user_words:
        return f"Вопрос: {first_item.get('question', '')}\nОтвет: {first_item.get('answer', '')}"
      
    best_score = 0
    best_item = first_item
    
    for item in faq_items:
        if not isinstance(item, dict):
            continue
        q_text = item.get('question', '')
        a_text = item.get('answer', '')
        
        q_words = set(q_text.lower().split())
        a_words = set(a_text.lower().split())
        item_words = q_words.union(a_words)
        
        score = len(user_words.intersection(item_words))
        if score > best_score:
            best_score = score
            best_item = item
              
    if best_score > 0:
        return f"Вопрос: {best_item.get('question', '')}\nОтвет: {best_item.get('answer', '')}"
    else:
        # Если точных совпадений нет, возвращаем первый вопрос или общую информацию
        return f"Вопрос: {first_item.get('question', '')}\nОтвет: {first_item.get('answer', '')}"

def get_groq_answer(user_message: str) -> str:
    retrieved_context = find_best_match(user_message)
    system_prompt = (
        "Ты вежливый и компетентный ассистент приемной комиссии РГСУ. "
        "Твоя задача — консультировать абитуриентов ТОЛЬКО на основе предоставленного контекста. "
        "Не придумывай информацию. Если ответа нет в контексте, отправь контакты ПК РГСУ: +7-495-255-67-67.\n\n"
        f"КОНТЕКСТ ИЗ БАЗЫ ЗНАНИЙ:\n{retrieved_context}"
    )
      
    response = groq_client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        model="openai/gpt-oss-120b",
        temperature=0.2,
        max_tokens=350
    )
    return response.choices[0].message.content

def send_message_to_max(target_id: str, text: str):
    if not MAX_BOT_TOKEN:
        print("Ошибка: MAX_BOT_TOKEN пустой!")
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
            print(f"Ответ МАКС при отправке ({id_param}): {res.status_code} {res.text}")
            if res.status_code == 200:
                break
        except Exception as e:
            print(f"Ошибка отправки сообщения в МАКС ({id_param}): {e}")

@app.get("/")
def root():
    return {"status": "RGSU Bot is running online!"}

@app.api_route("/webhook", methods=["GET", "POST"])
async def max_webhook(request: Request):
    if request.method == "GET":
        return {"status": "Webhook is active!"}
      
    try:
        body_bytes = await request.body()
        data = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
          
        print("=== ПОЛНЫЙ JSON ОТ МАКС ===")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        print("============================")
          
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

        if not message_text or not chat_id:
            print(f"Пропуск: текст='{message_text}', chat_id='{chat_id}'")
            return {"status": "ok"}

        print(f"УСПЕХ! ID получателя: {chat_id}, Текст: {message_text}")

        if message_text.lower() == "/start":
            reply = "Здравствуйте! Я цифровой ассистент приемной комиссии РГСУ. Задайте мне любой вопрос о поступлении!"
            send_message_to_max(str(chat_id), reply)
            return {"status": "ok"}

        # Генерация ответа через модель Groq и отправка в МАКС
        bot_reply = get_groq_answer(message_text)
        send_message_to_max(str(chat_id), bot_reply)
          
    except Exception as e:
        print(f"ОШИБКА В WEBHOOK: {type(e).__name__}: {e}")
        traceback.print_exc()
          
    return {"status": "ok"}
