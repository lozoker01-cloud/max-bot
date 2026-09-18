import os
import json
import requests
from fastapi import FastAPI, Request
from groq import Groq

# Переменные окружения
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
MAX_API_URL = "https://platform-api.max.ru/v1/messages/send"

app = FastAPI()
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

print("Загрузка базы знаний...")
with open('faq_data.json', 'r', encoding='utf-8') as f:
    faq_data = json.load(f)
print("База знаний успешно загружена в память!")

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

    response = groq_client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        model="llama-3.1-8b-instant",
        temperature=0.2,
        max_tokens=350
    )
    return response.choices[0].message.content

def send_message_to_max(chat_id: str, text: str):
    if not MAX_BOT_TOKEN:
        return
    headers = {
        "Authorization": f"Bearer {MAX_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    try:
        requests.post(MAX_API_URL, headers=headers, json=payload, timeout=5)
    except Exception as e:
        print(f"Ошибка отправки сообщения: {e}")

@app.get("/")
def root():
    return {"status": "RGSU Bot is running online!"}

# Обрабатываем и GET (для проверки связи от МАКС), и POST (для сообщений)
@app.api_route("/webhook", methods=["GET", "POST"])
async def max_webhook(request: Request):
    if request.method == "GET":
        return {"status": "Webhook is active and ready for POST requests!"}
    
    try:
        data = await request.json()
        message_obj = data.get("message", {})
        message_text = message_obj.get("text", "")
        chat_id = message_obj.get("chat_id", "")
        
        if not message_text or not chat_id:
            return {"status": "ok"}

        if message_text.lower() == "/start":
            reply = "Здравствуйте! Я цифровой ассистент приемной комиссии РГСУ. Задайте мне любой вопрос о поступлении!"
            send_message_to_max(chat_id, reply)
            return {"status": "ok"}

        bot_reply = get_groq_answer(message_text)
        send_message_to_max(chat_id, bot_reply)
        
    except Exception as e:
        print(f"Ошибка обработки: {e}")
        
    return {"status": "ok"}
