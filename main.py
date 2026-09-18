import os
import json
import faiss
import requests
from fastapi import FastAPI, Request
from groq import Groq
from sentence_transformers import SentenceTransformer

# Переменные окружения (зададим их в панели Render)
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
MAX_API_URL = "https://platform-api.max.ru/v1/messages/send"  # Уточнить по API MAX

app = FastAPI()

# Инициализация Groq
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# Загрузка векторной модели и базы
print("Загрузка модели поиска...")
embedder = SentenceTransformer('all-MiniLM-L6-v2')

with open('faq_data.json', 'r', encoding='utf-8') as f:
    faq_data = json.load(f)

faq_texts = [f"Вопрос: {item['question']}\nОтвет: {item['answer']}" for item in faq_data]
embeddings = embedder.encode(faq_texts)
dimension = embeddings.shape[1]

index = faiss.IndexFlatL2(dimension)
index.add(embeddings)
print("Векторная база готова!")


def get_groq_answer(user_message: str) -> str:
    # Поиск 2 самых релевантных совпадений из скрипта
    query_vector = embedder.encode([user_message])
    distances, indices = index.search(query_vector, 2)
    
    retrieved_context = "\n\n".join([faq_texts[i] for i in indices[0] if i < len(faq_texts)])
    
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


@app.post("/webhook")
async def max_webhook(request: Request):
    data = await request.json()
    
    try:
        # Структура входящего Webhook от MAX
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