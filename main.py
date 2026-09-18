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

print("Загрузка базы знаний (FAQ)...")
try:
    with open('faq_data.json', 'r', encoding='utf-8') as f:
        faq_raw = json.load(f)
    print("FAQ успешно загружен!")
except Exception as e:
    print(f"Ошибка загрузки faq_data.json: {e}")
    faq_raw = {}

print("Загрузка правил приема...")
pravila_text = ""
try:
    with open('pravila.txt', 'r', encoding='utf-8') as f:
        pravila_text = f.read()
    print("Правила приема успешно загружены!")
except Exception as e:
    print(f"Файл pravila.txt не найден (это не критично): {e}")

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

def find_top_matches(user_message: str, top_n: int = 3) -> str:
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

    return "\n\n---\n\n".join(top_matches)

def get_groq_answer(user_message: str) -> str:
    retrieved_faq = find_top_matches(user_message, top_n=3)
    
    # Жесткое требование отвечать сплошным текстом без таблиц и разметки
    system_prompt = (
        "Ты — цифровой ассистент приемной комиссии РГСУ. "
        "Отвечай на вопросы абитуриентов ТОЛЬКО сплошным, связным текстом (обычными абзацами). "
        "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать таблицы, списки с символами вроде '|', заголовки (##), символы разметки вроде '---' и любые другие спецсимволы форматирования. "
        "Пиши ответ так, будто отправляешь обычное текстовое сообщение в мессенджере. "
        "Используй информацию из базы Частых Вопросов (FAQ) и Официальных Правил Приема. "
        "Если ответа нет в контексте, вежливо сообщи об этом и укажи контакты ПК РГСУ: +7-495-255-67-67, почта pk@rgsu.net.\n\n"
        f"=== ЧАСТЫЕ ВОПРОСЫ (FAQ) ===\n{retrieved_faq}\n\n"
        f"=== ОФИЦИАЛЬНЫЕ ПРАВИЛА ПРИЕМА ===\n{pravila_text[:3000]}"
    )
      
    response = groq_client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        model="openai/gpt-oss-120b",
        temperature=0.3,
        max_tokens=1000  # Увеличен лимит токенов, чтобы текст не обрывался
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
            return {"status": "ok"}

        print(f"УСПЕХ! ID получателя: {chat_id}, Текст: {message_text}")

        if message_text.lower() == "/start":
            reply = "Здравствуйте! Я цифровой ассистент приемной комиссии РГСУ. Задайте мне любой вопрос о поступлении!"
            send_message_to_max(str(chat_id), reply)
            return {"status": "ok"}

        bot_reply = get_groq_answer(message_text)
        send_message_to_max(str(chat_id), bot_reply)
          
    except Exception as e:
        print(f"ОШИБКА В WEBHOOK: {type(e).__name__}: {e}")
        traceback.print_exc()
          
    return {"status": "ok"}
