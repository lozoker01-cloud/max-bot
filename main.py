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

print("Загрузка правил приема и скриптов...")
pravila_text = ""
try:
    with open('pravila.txt', 'r', encoding='utf-8') as f:
        # Считываем до 25 000 символов, чтобы охватить все слитые скрипты и правила
        pravila_text = f.read()[:25000] 
    print("Правила приема успешно загружены!")
except Exception as e:
    print(f"Файл pravila.txt не найден: {e}")

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
        "ТВОЙ АЛГОРИТМ РАБОТЫ ПЕРЕД ОТВЕТОМ (выполняй внутренне, пользователю выдавай только финальный результат):\n"
        "1. Проанализируй ОФИЦИАЛЬНЫЕ ПРАВИЛА ПРИЕМА (ниже).\n"
        "2. Проанализируй СКРИПТЫ И ЧАСТЫЕ ВОПРОСЫ (ниже).\n"
        "3. Сформируй ответ ТОЛЬКО на основе совпадений из этих двух блоков.\n\n"
        "ПРАВИЛА ФОРМИРОВАНИЯ ОТВЕТА:\n"
        "- КРАТКОСТЬ: Ответ должен состоять из 2-4 предложений сплошного текста. Никаких таблиц, заголовков или длинных списков.\n"
        "- СЛОЖНЫЕ ВОПРОСЫ: Если вопрос требует расчетов, спорный, нестандартный или состоит из множества условий, дай краткий базовый ответ и ОБЯЗАТЕЛЬНО добавь фразу: «Для разбора вашей ситуации свяжитесь с приёмной комиссией: +7-495-255-67-67 или pk@rgsu.net».\n"
        "- ЗАПРЕТ НА ФАНТАЗИИ: Если информации нет в предоставленных правилах или скриптах, отвечай строго: «К сожалению, у меня нет точной информации по данному вопросу. Пожалуйста, обратитесь напрямую в приемную комиссию по телефону +7-495-255-67-67».\n"
        "- СТИЛЬ: Живой, вежливый, понятный сплошной текст.\n\n"
        f"=== СКРИПТЫ И ЧАСТЫЕ ВОПРОСЫ (FAQ) ===\n{retrieved_faq}\n\n"
        f"=== ОФИЦИАЛЬНЫЕ ПРАВИЛА ПРИЕМА РГСУ ===\n{pravila_text}"
    )
      
    response = groq_client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        model="openai/gpt-oss-120b",
        temperature=0.1, 
        max_tokens=250 # Ограничиваем токенами, чтобы ответ физически не мог быть слишком длинным
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

        if message_text.lower() in ["/start", "старт", "привет"]:
            reply = "Здравствуйте! Я цифровой ассистент приемной комиссии РГСУ. Задайте мне любой вопрос о поступлении!"
            send_message_to_max(str(chat_id), reply)
            return {"status": "ok"}

        bot_reply = get_groq_answer(message_text)
        send_message_to_max(str(chat_id), bot_reply)
          
    except Exception as e:
        print(f"ОШИБКА В WEBHOOK: {type(e).__name__}: {e}")
        traceback.print_exc()
          
    return {"status": "ok"}
