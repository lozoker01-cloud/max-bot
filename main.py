import os
import json
import requests
import urllib3
import traceback
import re
import math
from fastapi import FastAPI, Request, BackgroundTasks

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Жесткая очистка ключей
raw_groq_key = os.getenv("GROQ_API_KEY", "")
GROQ_API_KEY = raw_groq_key.replace("\n", "").replace("\r", "").strip()

raw_max_token = os.getenv("MAX_BOT_TOKEN", "")
MAX_BOT_TOKEN = raw_max_token.replace("\n", "").replace("\r", "").strip()

MAX_API_BASE = "https://platform-api2.max.ru"
OPERATOR_ID = "20195632" 

# =====================================================================
# 🛑 ВСТАВЬТЕ СЮДА ВАШУ АКТУАЛЬНУЮ ССЫЛКУ ИЗ GOOGLE APPS SCRIPT
# =====================================================================
GOOGLE_SHEET_WEBHOOK = "https://script.google.com/macros/s/AKfycbzg8uTKzlKJkMY1MeRsg_FFYuv2LTHwuwdp5tlFNh4yn2y_pBzG-kzWDLANRwuQKv-dFQ/exec"

app = FastAPI()

user_sessions = {}
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

def get_embeddings_batch(texts: list) -> list:
    """Получает математические векторы для текста через Groq API"""
    if not GROQ_API_KEY or not texts: return []
    url = "https://api.groq.com/openai/v1/embeddings"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"input": texts, "model": "nomic-embed-text-v1_5"}
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=20)
        if res.status_code == 200:
            return [item["embedding"] for item in res.json().get("data", [])]
    except Exception as e:
        print("Ошибка векторизации:", e)
    return []

def load_knowledge_base():
    global faq_items
    faq_items = []
    
    print("Загрузка 1/3: faq_data.json...")
    try:
        with open('faq_data.json', 'r', encoding='utf-8') as f:
            faq_items.extend(flatten_faq(json.load(f)))
    except: pass

    print("Загрузка 2/3: pravila.txt...")
    try:
        with open('pravila.txt', 'r', encoding='utf-8') as f:
            for p in [p.strip() for p in f.read().split('\n\n') if len(p.strip()) > 20]:
                faq_items.append({"question": "Официальные правила", "answer": p})
    except: pass

    print("Загрузка 3/3: Google Таблицы...")
    if GOOGLE_SHEET_WEBHOOK and "AKfycbvH9" not in GOOGLE_SHEET_WEBHOOK:
        try:
            res = requests.get(GOOGLE_SHEET_WEBHOOK, timeout=10)
            if res.status_code == 200:
                for item in res.json():
                    faq_items.append({"question": item.get("question", ""), "answer": item.get("answer", "")})
        except: pass
            
    print(f"База загружена! Создаем векторы (Embeddings) для {len(faq_items)} блоков...")
    
    # Пакетная векторизация базы знаний (по 50 блоков за раз)
    batch_size = 50
    for i in range(0, len(faq_items), batch_size):
        batch = faq_items[i:i+batch_size]
        texts_to_embed = [f"Вопрос: {item.get('question','')} Ответ: {item.get('answer','')}" for item in batch]
        embeddings = get_embeddings_batch(texts_to_embed)
        if embeddings and len(embeddings) == len(batch):
            for j, emb in enumerate(embeddings):
                batch[j]['embedding'] = emb
                
    print("✅ Векторная база знаний готова!")

def register_webhook():
    if not MAX_BOT_TOKEN: return
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if not render_url: return
    try:
        requests.post(f"{MAX_API_BASE}/subscriptions", 
                      headers={"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}, 
                      json={"url": f"{render_url}/webhook", "update_types": ["message_created"]}, verify=False, timeout=10)
    except: pass

@app.on_event("startup")
def startup_event():
    load_knowledge_base()
    register_webhook()

def clean_text(text: str) -> str:
    return re.sub(r"\[" + "c" + "ite:" + r"\s*\d+\]", "", str(text)).strip()

def cosine_similarity(v1, v2):
    """Вычисляет семантическую близость между двумя текстами"""
    if not v1 or not v2: return 0.0
    dot_product = sum(a * b for a, b in zip(v1, v2))
    norm_v1 = math.sqrt(sum(a * a for a in v1))
    norm_v2 = math.sqrt(sum(b * b for b in v2))
    if norm_v1 == 0 or norm_v2 == 0: return 0.0
    return dot_product / (norm_v1 * norm_v2)

def find_top_matches(search_query: str, top_n: int = 2) -> str:
    global faq_items
    if not faq_items: return ""

    # Переводим запрос абитуриента в вектор
    query_vectors = get_embeddings_batch([search_query])
    query_emb = query_vectors[0] if query_vectors else None

    scored_items = []
    
    # Гибридный поиск: Векторный (основной) + Текстовый (запасной)
    for item in faq_items:
        score = 0
        if query_emb and 'embedding' in item:
            # 1. СМАРТ ПОИСК: Вычисляем смысловую близость
            score = cosine_similarity(query_emb, item['embedding'])
            score = score * 100 # Для удобства шкалы
        else:
            # 2. ЗАПАСНОЙ ПОИСК: Если сервер эмбеддингов недоступен
            clean_msg = re.sub(r'[^\w\s]', ' ', search_query.lower())
            words = clean_msg.split()
            stop_words = {"что", "такое", "как", "где", "когда", "есть", "ли", "это", "кто", "могу", "мне", "меня"}
            keywords = [w for w in words if len(w) > 2 and w not in stop_words]
            full_text = clean_text(item.get('question', '')).lower() + " " + clean_text(item.get('answer', '')).lower()
            for kw in keywords:
                if (kw[:-2] if len(kw) >= 5 else kw) in full_text: score += 1
            if clean_msg in full_text: score += 10
            
        # Отсекаем мусор (если близость ниже 30%)
        if score > 30 or (not query_emb and score > 0):
            scored_items.append((score, item))

    scored_items.sort(key=lambda x: x[0], reverse=True)
    
    top_matches = []
    current_length = 0
    MAX_CHARS = 3500

    for score, item in scored_items[:top_n]:
        match_text = f"ФАКТ: {clean_text(item.get('question', ''))}\nДЕТАЛИ: {clean_text(item.get('answer', ''))}\n\n"
        if current_length + len(match_text) > MAX_CHARS:
            allowed = MAX_CHARS - current_length
            if allowed > 100: top_matches.append(match_text[:allowed] + "... [ОБРЕЗАНО]")
            break
        top_matches.append(match_text)
        current_length += len(match_text)

    return "".join(top_matches)

def get_groq_answer(chat_id: str, user_message: str) -> str:
    if chat_id not in user_sessions:
        user_sessions[chat_id] = []
    
    history = user_sessions[chat_id]

    # Склеиваем вопрос с историей для умного поиска
    search_query = user_message
    if len(history) >= 2 and len(user_message.split()) <= 5:
        search_query = f"{history[-2]['content']} {user_message}"
        
    retrieved_faq = find_top_matches(search_query, top_n=2)
    
    if not retrieved_faq:
        return "К сожалению, у меня нет точной информации по данному вопросу."
    
    greeting = "1. БЕЗ ПРИВЕТСТВИЙ: Диалог уже идет, отвечай сразу по делу."
    if not history:
        greeting = "1. ПОЗДОРОВАЙСЯ: Это первый вопрос, начни вежливо."

    system_prompt = (
        "Ты — дружелюбный и компетентный консультант приемной комиссии РГСУ.\n"
        "ПРАВИЛА ОТВЕТА:\n"
        f"{greeting}\n"
        "2. УЧИТЫВАЙ КОНТЕКСТ: Анализируй предыдущие сообщения, чтобы понимать, о чем речь.\n"
        "3. БАЗА ЗНАНИЙ: Отвечай ТОЛЬКО опираясь на факты ниже. Если информации нет, так и скажи.\n\n"
        f"=== АКТУАЛЬНЫЕ ФАКТЫ ИЗ БАЗЫ (ВЕКТОРНЫЙ ПОИСК) ===\n{retrieved_faq}"
    )
      
    if not GROQ_API_KEY: return "🚨 ОШИБКА: Не задан GROQ_API_KEY."

    messages_for_api = [{"role": "system", "content": system_prompt}]
    for msg in history[-4:]:
        messages_for_api.append(msg)
    messages_for_api.append({"role": "user", "content": user_message})

    try:
        res = requests.post("https://api.groq.com/openai/v1/chat/completions", 
                            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}, 
                            json={"model": "openai/gpt-oss-120b", "messages": messages_for_api, "temperature": 0.2, "max_tokens": 500}, 
                            timeout=15)
        data = res.json()
        if res.status_code == 200:
            answer = data["choices"][0]["message"]["content"]
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": answer})
            if len(history) > 6: user_sessions[chat_id] = history[-6:]
            return answer
        else:
            return f"🚨 ОШИБКА GROQ: {data.get('error', {}).get('message', str(data))}"
    except Exception as e:
        return f"🚨 ОШИБКА ЗАПРОСА: {str(e)}"

def send_message_to_max(target_id: str, text: str):
    if not MAX_BOT_TOKEN or not target_id: return
    try:
        requests.post(f"{MAX_API_BASE}/messages", headers={"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}, 
                      params={"chat_id": target_id}, json={"chat_id": target_id, "text": text}, verify=False, timeout=5)
    except: pass

def log_to_google_sheet(user_name, user_id, question, answer):
    if not GOOGLE_SHEET_WEBHOOK or "AKfycbvH9" in GOOGLE_SHEET_WEBHOOK: return
    try:
        requests.post(GOOGLE_SHEET_WEBHOOK, json={"name": user_name, "id": user_id, "question": question, "answer": answer}, timeout=15)
    except: pass

def transfer_to_operator(user_id: str, user_name: str):
    send_message_to_max(user_id, "⏳ Переключаю вас на специалиста приемной комиссии. Пожалуйста, подождите, скоро вам ответят.")
    last_q, last_a = "Неизвестно", "Нет ответа"
    if user_id in user_sessions and len(user_sessions[user_id]) >= 2:
        last_q = user_sessions[user_id][-2]["content"]
        last_a = user_sessions[user_id][-1]["content"]
    if OPERATOR_ID:
        send_message_to_max(OPERATOR_ID, f"🚨 ЗАПРОС ОПЕРАТОРА\n\n👤 Имя: {user_name}\n🆔 ID: {user_id}\n\n❓ Вопрос:\n{last_q}\n\n🤖 Ответ бота:\n{last_a}\n\n👇 Для ответа скопируйте:\n\n/ответ {user_id} ")

def process_user_message(chat_id: str, text: str, user_name: str):
    if str(chat_id) == str(OPERATOR_ID) and text.lower().startswith("/ответ"):
        parts = text.split(" ", 2)
        if len(parts) >= 3:
            send_message_to_max(parts[1], f"👨‍💻 *Сообщение от специалиста приемной комиссии:*\n\n{parts[2]}")
            send_message_to_max(chat_id, f"✅ Ответ отправлен пользователю {parts[1]}!")
        return

    if text.strip().lower() == "/myid":
        send_message_to_max(chat_id, f"✅ Ваш внутренний ID:\n{chat_id}")
        return

    # ОТЛОВ РАЗГОВОРНЫХ ФРАЗ БЕЗ ЗАПРОСА В БАЗУ И НЕЙРОСЕТЬ
    clean_text_lower = re.sub(r'[^\w\s]', '', text.lower()).strip()
    if clean_text_lower in ["спасибо", "спс", "благодарю", "понял", "ок", "хорошо", "ясно", "понятно", "супер", "отлично"]:
        send_message_to_max(chat_id, "Рад был помочь! Если появятся еще вопросы — обращайтесь. Удачи с поступлением!")
        log_to_google_sheet(user_name, chat_id, text, "Рад был помочь (Авто-ответ)")
        return

    if text.lower() in ["/start", "старт"]:
        send_message_to_max(chat_id, "Здравствуйте! Я цифровой ассистент приемной комиссии РГСУ. Задайте мне любой вопрос о поступлении, и я постараюсь помочь!")
        user_sessions[chat_id] = []
        return
        
    if "оператор" in text.lower():
        transfer_to_operator(chat_id, user_name)
        return

    bot_reply = get_groq_answer(chat_id, text)
    
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
    return {"status": "Bot is running with Semantic Vector Search (Embeddings) & Memory!"}

@app.get("/reload_faq")
def api_reload_faq():
    load_knowledge_base()
    return {"status": "success", "rules_loaded": len(faq_items)}

@app.api_route("/webhook", methods=["GET", "POST"])
async def max_webhook(request: Request, background_tasks: BackgroundTasks):
    if request.method == "GET": return {"status": "Webhook is active!"}
    try:
        data = json.loads((await request.body()).decode("utf-8")) or {}
        msg = data.get("message", {})
        text = msg.get("body", {}).get("text") or msg.get("text") or data.get("text") or ""
        sender = msg.get("sender", {})
        user_name = sender.get("name") or sender.get("username") or "Абитуриент"
        chat_id = msg.get("chat_id") or sender.get("user_id") or sender.get("id") or msg.get("recipient", {}).get("chat_id") or data.get("chat_id") or data.get("user_id") or ""
        
        if not chat_id and isinstance(msg, dict):
            for v in msg.values():
                if isinstance(v, dict) and "id" in v:
                    chat_id = v["id"]
                    break

        if text and chat_id:
            background_tasks.add_task(process_user_message, str(chat_id), text, str(user_name))
    except: pass
    return {"status": "ok"}
