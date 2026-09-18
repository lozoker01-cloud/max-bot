import os
import json
import requests
import urllib3
from fastapi import FastAPI, Request
from groq import Groq

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

def find_best_matches(user_message: str, top_k: int = 3) -> str:
    """Ищет несколько наиболее подходящих по смыслу/ключевым словам вопросов в базе знаний"""
    user_words = set(user_message.lower().split())
    # Убираем слишком короткие слова
    user_words = {w for w in user_words if len(w) > 2}
    
    if not user_words:
        return f"Вопрос: {faq_data[0]['question']}\nОтвет: {faq_data[0]['answer']}"
    
    scored_items = []
    for item in faq_data:
        q_words = set(item['question'].lower().split())
        a_words = set(item['answer'].lower().split())
        item_words = q_words.union(a_words)
        
        score = len(user_words.intersection(item_words))
        scored_items.append((score, item))
    
    # Сортируем по убыванию релевантности
    scored_items.sort(key=lambda x: x[0], reverse=True)
    
    # Берем топ-к результатов с ненулевым совпадением (или хотя бы лучшие)
    best_matches = [item for score, item in scored_items[:top_k] if score > 0]
    
    if not best_matches:
        # Если совпадений нет вообще, возвращаем общую информацию
        best_matches = [faq_data[0]]
        
    context_text = ""
    for idx, item in enumerate(best_matches, 1):
        context_text += f"Фрагмент {idx}:\nВопрос: {item['question']}\nОтвет: {item['answer']}\n\n"
        
    return context_text

def get_groq_answer(user_message: str) -> str:
    retrieved_context = find_best_matches(user_message, top_k=3)
    system_prompt = (
        "Ты вежливый и компетентный цифровой ассистент приемной комиссии РГСУ. "
        "Твоя задача — консультировать абитуриентов своими словами на основе предоставленных фрагментов из базы знаний. "
        "Пользователи могут задавать вопросы в свободной форме (с синонимами, сленгом или неточностями). Твоя цель — понять суть вопроса, сопоставить с контекстом и дать развернутый, точный ответ. "
        "Не придумывай информацию, которой нет в контексте. Если точного ответа в контексте нет, вежливо предложи обратиться в приемную комиссию РГСУ по телефону: +7-495-255-67-67.\n\n"
        f"БАЗА ЗНАНИЙ (ФРАГМЕНТЫ):\n{retrieved_context}"
    )
    
    response = groq_client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        model="openai/gpt-oss-120b",
        temperature=0.3,
        max_tokens=400
    )
    return response.choices[0].message.content

def send_message_to_max(chat_id: str, text: str):
    if not MAX_BOT_TOKEN:
        print("Ошибка: MAX_BOT_TOKEN пустой!")
        return
    headers = {
        "Authorization": f"{MAX_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "text": text,
        "chat_id": chat_id
    }
    
    try:
        res = requests.post(f"{MAX_API_BASE}/messages", headers=headers, params={"chat_id": chat_id}, json=payload, verify=False, timeout=5)
        print(f"Ответ МАКС при отправке сообщения: {res.status_code} {res.text}")
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
            msg_block.get("recipient", {}).get("chat_id") or
            msg_block.get("sender", {}).get("id") or 
            data.get("chat_id") or 
            ""
        )
        
        if not message_text or not chat_id:
            print(f"Пропуск: текст='{message_text}', chat_id='{chat_id}'")
            return {"status": "ok"}

        print(f"УСПЕХ! Чат: {chat_id}, Текст: {message_text}")

        if message_text.lower() == "/start":
            reply = "Здравствуйте! Я цифровой ассистент приемной комиссии РГСУ. Задайте мне любой вопрос о поступлении, баллах, общежитии или документах!"
            send_message_to_max(str(chat_id), reply)
            return {"status": "ok"}

        # Умная генерация ответа через Groq с использованием топ-3 релевантных фрагментов
        bot_reply = get_groq_answer(message_text)
        send_message_to_max(str(chat_id), bot_reply)
        
    except Exception as e:
        print(f"ОШИБКА В WEBHOOK: {e}")
        
    return {"status": "ok"}
