import os
import json
import requests
import urllib3
from fastapi import FastAPI, Request
from groq import Groq

# Отключаем предупреждения об отключенном SSL для запросов к API МАКС (сертификаты Минцифры)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")

MAX_API_BASE = "https://platform-api2.max.ru"

app = FastAPI()
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

print("Загрузка базы знаний...")
with open('faq_data.json', 'r', encoding='utf-8') as f:
    faq_data = json.load(f)
print("База знаний успешно загружена!")

def register_webhook():
    """Автоматическая регистрация вебхука в МАКС при старте сервера"""
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
    user_words = set(user_message.lower().split())
    user_words = {w for w in user_words if len(w) > 2}
    if not user_words:
        return faq_data[0]['answer']
    
    best_score = 0
    best_item = faq_data[0]
    for item in faq_data:
        q_words = set(item['question'].lower().split())
        a_words = set(item['answer'].lower().split())
        item_words = q_words.union(a_words)
        score = len(user_words.intersection(item_words))
        if score > best_score:
            best_score = score
            best_item = item
            
    if best_score > 0:
        return f"Вопрос: {best_item['question']}\nОтвет: {best_item['answer']}"
    else:
        return f"Вопрос: {faq_data[0]['question']}\nОтвет: {faq_data[0]['answer']}"

def get_groq_answer(user_message: str) -> str:
    retrieved_context = find_best_match(user_message)
    system_prompt = (
        "Ты вежливый и компетентный ассистент приемной комиссии РГСУ. "
        "Твоя задача — консультировать абитуриентов ТОЛЬКО на основе предоставленного контекста. "
        "Не придумывай информацию. Если ответа нет в контексте, отправь контакты ПК РГСУ: +7-495-255-67-67.\n\n"
        f"КОНТЕКСТ ИЗ БАЗЫ ЗНАНИЙ:\n{retrieved_context}"
    )
    
    # Используем модель, указанную тобой
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

def send_message_to_max(chat_id: str, text: str):
    if not MAX_BOT_TOKEN:
        return
    headers = {
        "Authorization": f"{MAX_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    params = {"chat_id": chat_id}
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    try:
        requests.post(f"{MAX_API_BASE}/messages", headers=headers, params=params, json=payload, verify=False, timeout=5)
    except Exception as e:
        print(f"Ошибка отправки сообщения в МАКС: {e}")

@app.get("/")
def root():
    return {"status": "RGSU Bot is running online!"}

@app.api_route("/webhook", methods=["GET", "POST"])
async def max_webhook(request: Request):
    if request.method == "GET":
        return {"status": "Webhook is active!"}
    
    try:
        data = await request.json()
        message_obj = data.get("object", data.get("message", data))
        message_text = message_obj.get("text", message_obj.get("body", {}).get("text", ""))
        chat_id = message_obj.get("chat_id", message_obj.get("from", {}).get("id", ""))
        
        if not message_text:
            return {"status": "ok"}

        if message_text.lower() == "/start":
            reply = "Здравствуйте! Я цифровой ассистент приемной комиссии РГСУ. Задайте мне любой вопрос о поступлении!"
            send_message_to_max(str(chat_id), reply)
            return {"status": "ok"}

        bot_reply = get_groq_answer(message_text)
        send_message_to_max(str(chat_id), bot_reply)
        
    except Exception as e:
        print(f"Ошибка внутри webhook: {e}")
        
    return {"status": "ok"}
