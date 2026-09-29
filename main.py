import os
import json
import requests
import urllib3
import re
import traceback
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# --- НОВЫЕ БИБЛИОТЕКИ ДЛЯ RAG И PDF ---
import PyPDF2
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain.schema import Document

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

raw_groq_key = os.getenv("GROQ_API_KEY", "")
GROQ_API_KEY = raw_groq_key.replace("\n", "").replace("\r", "").strip()

raw_max_token = os.getenv("MAX_BOT_TOKEN", "")
MAX_BOT_TOKEN = raw_max_token.replace("\n", "").replace("\r", "").strip()

MAX_API_BASE = "https://platform-api2.max.ru"

# СПИСОК ID ОПЕРАТОРОВ
OPERATOR_IDS = ["20195632", "332287012"] 

# ВАШ WEBHOOK GOOGLE ТАБЛИЦ
GOOGLE_SHEET_WEBHOOK = "https://script.google.com/macros/s/AKfycbzg8uTKzlKJkMY1MeRsg_FFYuv2LTHwuwdp5tlFNh4yn2y_pBzG-kzWDLANRwuQKv-dFQ/exec"

app = FastAPI()

user_sessions = {}
active_tickets = {}       
operator_target = {}

# =====================================================================
# 🧠 RAG СИСТЕМА: ВЕКТОРНАЯ БАЗА И ЧТЕНИЕ PDF
# =====================================================================
vector_db = None

# Используем легкую и лучшую модель для русского языка (почти не ест оперативку)
try:
    embedder = HuggingFaceEmbeddings(model_name="cointegrated/rubert-tiny2")
except Exception as e:
    print(f"Ошибка загрузки модели эмбеддингов: {e}")
    embedder = None

def build_rag_database():
    global vector_db
    print("🔄 Создание векторной базы (RAG)...")
    docs_dir = "docs"
    
    # Создаем папку docs, если её нет
    if not os.path.exists(docs_dir):
        os.makedirs(docs_dir)

    raw_docs = []
    
    # 1. Читаем все PDF и TXT из папки docs
    for filename in os.listdir(docs_dir):
        filepath = os.path.join(docs_dir, filename)
        if filename.endswith(".pdf"):
            try:
                with open(filepath, "rb") as f:
                    reader = PyPDF2.PdfReader(f)
                    text = "".join([page.extract_text() + "\n" for page in reader.pages if page.extract_text()])
                    if text.strip():
                        raw_docs.append(Document(page_content=text, metadata={"source": filename}))
            except Exception as e:
                print(f"Ошибка чтения PDF {filename}: {e}")
                
        elif filename.endswith(".txt"):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    raw_docs.append(Document(page_content=f.read(), metadata={"source": filename}))
            except Exception as e:
                print(f"Ошибка чтения TXT {filename}: {e}")
                
    # 2. Заглушка, чтобы FAISS не упал, если папка пуста
    if not raw_docs:
        raw_docs.append(Document(page_content="База знаний пока пуста. Оператор подскажет.", metadata={"source": "system"}))
        
    # 3. Умная нарезка текста (чанкинг) с перекрытием контекста
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,       # Размер куска текста
        chunk_overlap=200,    # Перекрытие, чтобы не терять контекст на стыках
        separators=["\n\n", "\n", ".", " ", ""]
    )
    chunks = text_splitter.split_documents(raw_docs)
    
    # 4. Векторизация и создание FAISS
    if embedder:
        try:
            vector_db = FAISS.from_documents(chunks, embedder)
            print(f"✅ Векторная база готова! Создано {len(chunks)} фрагментов из документов.")
        except Exception as e:
            print(f"Ошибка создания FAISS: {e}")
    else:
        print("❌ Эмбеддер не загружен, RAG отключен.")

def semantic_search(query: str, k: int = 4) -> str:
    global vector_db
    if not vector_db: 
        return ""
    
    # Поиск самых подходящих по смыслу фрагментов
    try:
        results = vector_db.similarity_search(query, k=k)
        context = ""
        for idx, doc in enumerate(results):
            source = doc.metadata.get('source', 'unknown')
            context += f"--- ФРАГМЕНТ ИЗ ДОКУМЕНТА '{source}' ---\n{doc.page_content.strip()}\n\n"
        return context
    except Exception as e:
        print(f"Ошибка поиска: {e}")
        return ""

@app.on_event("startup")
def startup_event():
    build_rag_database()
    register_webhook()

@app.get("/reload_faq")
def api_reload_faq():
    build_rag_database()
    return {"status": "success", "message": "Документы успешно перечитаны и векторизованы!"}

# =====================================================================
# 🖥️ ВИЗУАЛЬНАЯ WEB-ПАНЕЛЬ ОПЕРАТОРА (Осталась без изменений)
# =====================================================================
class ReplyData(BaseModel):
    user_id: str
    text: str

HTML_PANEL = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Панель Оператора РГСУ</title>
    <style>
        * { box-sizing: border-box; }
        body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; display: flex; height: 100vh; background: #e5ddd5; color: #333; }
        #sidebar { width: 35%; min-width: 250px; background: #fff; border-right: 1px solid #d1d1d1; display: flex; flex-direction: column; }
        #sidebar-header { background: #f0f2f5; padding: 15px; font-weight: bold; border-bottom: 1px solid #d1d1d1; color: #54656f; }
        #ticket-list { flex: 1; overflow-y: auto; }
        .ticket { padding: 15px; border-bottom: 1px solid #f2f2f2; cursor: pointer; transition: background 0.2s; }
        .ticket:hover { background: #f5f6f6; }
        .ticket.active { background: #ebebeb; border-left: 4px solid #00a884; }
        .ticket-name { font-weight: bold; color: #111b21; margin-bottom: 5px; }
        .ticket-id { font-size: 12px; color: #667781; }
        
        #main-chat { flex: 1; display: flex; flex-direction: column; position: relative; }
        #chat-header { background: #f0f2f5; padding: 15px; border-bottom: 1px solid #d1d1d1; display: flex; justify-content: space-between; align-items: center; }
        #chat-header h3 { margin: 0; color: #111b21; font-size: 16px; }
        .close-btn { background: #ff3b30; color: #fff; border: none; padding: 8px 12px; border-radius: 6px; font-weight: bold; cursor: pointer; }
        
        #chat-window { flex: 1; padding: 20px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; background-image: url('https://user-images.githubusercontent.com/15075759/28719144-86dc0f70-73b1-11e7-911d-60d70fcded21.png'); background-color: #e5ddd5; }
        
        .msg { max-width: 75%; padding: 8px 12px; border-radius: 8px; font-size: 14px; line-height: 1.4; box-shadow: 0 1px 0.5px rgba(11,20,26,.13); position: relative; }
        .msg.user { background: #fff; align-self: flex-start; border-top-left-radius: 0; }
        .msg.bot { background: #dcf8c6; align-self: flex-end; border-top-right-radius: 0; }
        .msg.operator { background: #cce5ff; align-self: flex-end; border-top-right-radius: 0; border: 1px solid #b8daff; }
        
        #input-area { background: #f0f2f5; padding: 12px 20px; display: flex; gap: 10px; align-items: center; }
        #msg-input { flex: 1; padding: 12px; border: none; border-radius: 8px; outline: none; font-size: 15px; }
        #send-btn { background: #00a884; color: white; border: none; padding: 12px 20px; border-radius: 8px; font-weight: bold; cursor: pointer; }
        
        .overlay { position: absolute; top:0; left:0; right:0; bottom:0; background: #f0f2f5; display: flex; justify-content: center; align-items: center; font-size: 18px; color: #667781; z-index: 10; text-align: center; padding: 20px;}
    </style>
</head>
<body>
    <div id="sidebar">
        <div id="sidebar-header">⏳ Ожидают ответа</div>
        <div id="ticket-list"></div>
    </div>
    <div id="main-chat">
        <div id="no-chat-overlay" class="overlay">Выберите абитуриента из списка слева, чтобы начать общение</div>
        <div id="chat-header">
            <h3 id="current-name">Имя абитуриента</h3>
            <button class="close-btn" onclick="closeTicket()">Завершить диалог</button>
        </div>
        <div id="chat-window"></div>
        <div id="input-area">
            <input type="text" id="msg-input" placeholder="Введите ответ..." onkeypress="if(event.key === 'Enter') sendMessage()">
            <button id="send-btn" onclick="sendMessage()">Отправить</button>
        </div>
    </div>

    <script>
        let currentUserId = null;
        let lastMsgCount = 0;

        async function fetchTickets() {
            try {
                const res = await fetch('/api/tickets');
                const tickets = await res.json();
                const list = document.getElementById('ticket-list');
                list.innerHTML = '';
                let hasActive = false;
                for (const [id, info] of Object.entries(tickets)) {
                    hasActive = true;
                    const div = document.createElement('div');
                    div.className = 'ticket' + (currentUserId === id ? ' active' : '');
                    div.onclick = () => selectTicket(id, info.name);
                    div.innerHTML = `<div class="ticket-name">${info.name}</div><div class="ticket-id">ID: ${id}</div>`;
                    list.appendChild(div);
                }
                if (!hasActive) {
                    list.innerHTML = '<div style="padding: 20px; color: #666; text-align: center;">Нет активных обращений от абитуриентов</div>';
                }
            } catch (e) { console.error(e); }
        }

        async function fetchChat() {
            if (!currentUserId) return;
            try {
                const res = await fetch(`/api/chat/${currentUserId}`);
                const history = await res.json();
                if (history.length !== lastMsgCount) {
                    lastMsgCount = history.length;
                    const chat = document.getElementById('chat-window');
                    chat.innerHTML = '';
                    history.forEach(msg => {
                        const div = document.createElement('div');
                        if (msg.role === 'user') {
                            div.className = 'msg user';
                            div.innerText = msg.content;
                        } else {
                            if (msg.content.includes('👨‍💻 Оператор:')) {
                                div.className = 'msg operator';
                                div.innerText = msg.content.replace('👨‍💻 Оператор: ', '');
                            } else {
                                div.className = 'msg bot';
                                div.innerText = msg.content;
                            }
                        }
                        chat.appendChild(div);
                    });
                    chat.scrollTop = chat.scrollHeight;
                }
            } catch (e) { console.error(e); }
        }

        function selectTicket(id, name) {
            currentUserId = id;
            lastMsgCount = 0;
            document.getElementById('current-name').innerText = name;
            document.getElementById('no-chat-overlay').style.display = 'none';
            fetchTickets();
            fetchChat();
        }

        async function sendMessage() {
            const inp = document.getElementById('msg-input');
            const text = inp.value.trim();
            if (!text || !currentUserId) return;
            inp.value = '';
            
            const chat = document.getElementById('chat-window');
            const div = document.createElement('div');
            div.className = 'msg operator';
            div.innerText = text;
            chat.appendChild(div);
            chat.scrollTop = chat.scrollHeight;
            lastMsgCount++; 

            await fetch('/api/reply', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({user_id: currentUserId, text: text})
            });
            fetchChat();
        }

        async function closeTicket() {
            if (!currentUserId) return;
            if (!confirm('Точно закрыть диалог?')) return;
            await fetch(`/api/close/${currentUserId}`, { method: 'POST' });
            currentUserId = null;
            document.getElementById('no-chat-overlay').style.display = 'flex';
            fetchTickets();
        }

        setInterval(fetchTickets, 3000);
        setInterval(fetchChat, 2000);
        fetchTickets();
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def root():
    return HTMLResponse(content=HTML_PANEL)

@app.get("/api/tickets")
def api_get_tickets():
    return active_tickets

@app.get("/api/chat/{user_id}")
def api_get_chat(user_id: str):
    return user_sessions.get(user_id, [])

@app.post("/api/reply")
def api_reply(data: ReplyData):
    send_message_to_max(data.user_id, f"👨‍💻 *Специалист приемной комиссии:*\n\n{data.text}")
    if data.user_id in user_sessions:
        user_sessions[data.user_id].append({"role": "assistant", "content": f"👨‍💻 Оператор: {data.text}"})
    return {"status": "ok"}

@app.post("/api/close/{user_id}")
def api_close(user_id: str):
    if user_id in active_tickets:
        del active_tickets[user_id]
        send_message_to_max(user_id, "✅ Ваш диалог со специалистом завершен. Цифровой помощник снова к вашим услугам!")
    return {"status": "ok"}

# =====================================================================
# 🤖 ЛОГИКА БОТА И ВЗАИМОДЕЙСТВИЕ С GROQ И MAX
# =====================================================================

def register_webhook():
    if not MAX_BOT_TOKEN: return
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if not render_url: return
    try:
        requests.post(f"{MAX_API_BASE}/subscriptions", 
                      headers={"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}, 
                      json={"url": f"{render_url}/webhook", "update_types": ["message_created"]}, verify=False, timeout=10)
    except: pass

def get_groq_answer(chat_id: str, user_message: str) -> str:
    if chat_id not in user_sessions: user_sessions[chat_id] = []
    history = user_sessions[chat_id]

    # Умный семантический поиск по загруженным PDF и TXT документам
    search_query = user_message
    if len(history) >= 2 and len(user_message.split()) <= 6:
        search_query = f"{history[-2]['content']} {user_message}"
        
    retrieved_context = semantic_search(search_query, k=5)
    
    greeting = "1. БЕЗ ПРИВЕТСТВИЙ: Диалог уже идет, отвечай сразу по делу."
    if not history: greeting = "1. ПОЗДОРОВАЙСЯ: Это первый вопрос, начни вежливо."

    # ЖЕСТКИЙ ПРОМПТ ДЛЯ GROQ С ЗАПРЕТОМ ГАЛЛЮЦИНАЦИЙ
    system_prompt = (
        "Ты — официальный цифровой помощник приемной комиссии РГСУ.\n"
        "Твоя главная задача — давать ИДЕАЛЬНО ТОЧНЫЕ ответы на основе предоставленных фрагментов документов.\n\n"
        "🔴 СТРОГИЕ ПРАВИЛА:\n"
        f"{greeting}\n"
        "2. ОТВЕЧАЙ ТОЛЬКО НА ОСНОВЕ ФРАГМЕНТОВ НИЖЕ.\n"
        "3. Если в фрагментах НЕТ ответа на вопрос пользователя, КАТЕГОРИЧЕСКИ ЗАПРЕЩАЕТСЯ придумывать ответ. В этом случае скажи: «К сожалению, я не нашел точной информации в правилах. Напишите слово 'Оператор', и специалист вам поможет.»\n"
        "4. Отвечай кратко, понятно и по делу.\n\n"
        f"=== ФРАГМЕНТЫ ОФИЦИАЛЬНЫХ ДОКУМЕНТОВ ===\n{retrieved_context}"
    )
      
    if not GROQ_API_KEY: return "🚨 ОШИБКА: Не задан GROQ_API_KEY."

    messages_for_api = [{"role": "system", "content": system_prompt}]
    for msg in history[-4:]: 
        safe_msg = {"role": "assistant" if msg["role"] == "operator" else msg["role"], "content": msg["content"]}
        messages_for_api.append(safe_msg)
    
    messages_for_api.append({"role": "user", "content": user_message})

    try:
        res = requests.post("https://api.groq.com/openai/v1/chat/completions", 
                            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}, 
                            json={"model": "openai/gpt-oss-120b", "messages": messages_for_api, "temperature": 0.1, "max_tokens": 600}, 
                            timeout=20)
        data = res.json()
        if res.status_code == 200:
            answer = data["choices"][0]["message"]["content"]
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": answer})
            if len(history) > 50: user_sessions[chat_id] = history[-50:]
            return answer
        else:
            return f"🚨 ОШИБКА GROQ: {data.get('error', {}).get('message', str(data))}"
    except Exception as e:
        return f"🚨 ОШИБКА ЗАПРОСА: {str(e)}"

def send_message_to_max(target_id: str, text: str):
    if not MAX_BOT_TOKEN or not target_id: return
    headers = {"Authorization": f"{MAX_BOT_TOKEN}", "Content-Type": "application/json"}
    for id_param in ["user_id", "chat_id"]:
        try:
            res = requests.post(f"{MAX_API_BASE}/messages", headers=headers, params={id_param: target_id}, json={id_param: target_id, "text": text}, verify=False, timeout=5)
            if res.status_code == 200: break
        except: pass

def log_to_google_sheet(user_name, user_id, question, answer):
    if not GOOGLE_SHEET_WEBHOOK or "AKfycbvH9" in GOOGLE_SHEET_WEBHOOK: return
    try:
        requests.post(GOOGLE_SHEET_WEBHOOK, json={"name": user_name, "id": user_id, "question": question, "answer": answer}, timeout=15)
    except: pass

def transfer_to_operator(user_id: str, user_name: str):
    active_tickets[user_id] = {"name": user_name}
    send_message_to_max(user_id, f"⏳ {user_name}, переключаю вас на специалиста. Вся история диалога сохранена, ожидайте ответа.")
    alert_text = (f"🚨 НОВЫЙ ЗАПРОС ОТ АБИТУРИЕНТА!\n\n👤 Имя: *{user_name}*\n💬 Откройте меню (квадратики слева) и нажмите на сервис бота, чтобы ответить.")
    for op_id in OPERATOR_IDS:
        if op_id and op_id != "ВТОРОЙ_ID_СЮДА":
            send_message_to_max(str(op_id), alert_text)

@app.api_route("/webhook", methods=["GET", "POST"])
async def max_webhook(request: Request, background_tasks: BackgroundTasks):
    if request.method == "GET": return HTMLResponse(content=HTML_PANEL)
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

def process_user_message(chat_id: str, text: str, user_name: str):
    is_operator_command_handled = False 
    if str(chat_id) in [str(op) for op in OPERATOR_IDS]:
        text_lower = text.lower()
        if text_lower.startswith("/клиенты") or text_lower.startswith("/list"):
            if not active_tickets:
                send_message_to_max(chat_id, "📭 Сейчас нет активных обращений.")
                return
            list_msg = "📋 *Активные обращения:*\n\n"
            for u_id, info in active_tickets.items():
                list_msg += f"👤 *{info['name']}* (ID: `{u_id}`)\n"
            send_message_to_max(chat_id, list_msg)
            return
        elif text_lower.startswith("/о "):
            parts = text.split(" ", 1)
            if len(parts) >= 2:
                target_user = parts[1].strip()
                if target_user in active_tickets:
                    operator_target[str(chat_id)] = target_user
                    send_message_to_max(chat_id, f"✅ Вы закрепили абитуриента: *{active_tickets[target_user]['name']}*")
                else: send_message_to_max(chat_id, "❌ Абитуриент не найден.")
            return
        elif text_lower.startswith("/закрыть"):
            parts = text.split(" ", 1)
            target_user = parts[1].strip() if len(parts) >= 2 else operator_target.get(str(chat_id))
            if target_user and target_user in active_tickets:
                name = active_tickets[target_user]["name"]
                del active_tickets[target_user]
                if operator_target.get(str(chat_id)) == target_user: operator_target.pop(str(chat_id), None)
                send_message_to_max(target_user, "✅ Ваш диалог со специалистом завершен.")
                send_message_to_max(chat_id, f"🔒 Тикет с *{name}* закрыт.")
            return
        else:
            target_user = operator_target.get(str(chat_id))
            if target_user and target_user in active_tickets:
                client_name = active_tickets[target_user]["name"]
                send_message_to_max(target_user, f"👨‍💻 *Специалист приемной комиссии:*\n\n{text}")
                send_message_to_max(chat_id, f"↗️ [Вам ➔ {client_name}]: {text}")
                if target_user in user_sessions:
                    user_sessions[target_user].append({"role": "assistant", "content": f"👨‍💻 Оператор: {text}"})
                is_operator_command_handled = True

    if is_operator_command_handled: return

    if text.strip().lower() == "/myid":
        send_message_to_max(chat_id, f"✅ Ваш внутренний ID:\n{chat_id}")
        return

    # РЕЖИМ ЖИВОГО ДИАЛОГА ДЛЯ АБИТУРИЕНТА
    if chat_id in active_tickets:
        if chat_id not in user_sessions: user_sessions[chat_id] = []
        user_sessions[chat_id].append({"role": "user", "content": text})
        client_name = active_tickets[chat_id]["name"]
        live_alert = f"💬 *Сообщение от {client_name}*:\n\n{text}"
        for op_id in OPERATOR_IDS:
            if op_id and op_id != "ВТОРОЙ_ID_СЮДА": send_message_to_max(str(op_id), live_alert)
        return

    # РЕЖИМ УМНОГО ИИ-БОТА (RAG)
    clean_text_lower = re.sub(r'[^\w\s]', '', text.lower()).strip()
    if clean_text_lower in ["спасибо", "спс", "благодарю", "понял", "ок", "хорошо", "ясно", "понятно", "супер", "отлично"]:
        send_message_to_max(chat_id, "Рад был помочь! Если появятся еще вопросы — обращайтесь. Удачи с поступлением!")
        return

    if text.lower() in ["/start", "старт"]:
        send_message_to_max(chat_id, "Здравствуйте! Я цифровой ассистент приемной комиссии РГСУ. Задайте мне любой вопрос о поступлении, и я постараюсь помочь!")
        user_sessions[chat_id] = []
        if chat_id in active_tickets: del active_tickets[chat_id]
        return
        
    if "оператор" in text.lower():
        transfer_to_operator(chat_id, user_name)
        return

    # Запрос к Groq с векторным контекстом
    bot_reply = get_groq_answer(chat_id, text)
    
    if "🚨 ОШИБКА" not in bot_reply:
        if "к сожалению" not in bot_reply.lower() and "не нашел точной информации" not in bot_reply.lower():
            final_reply = bot_reply + "\n\n---\n*Если я не смог полностью ответить на ваш вопрос, напишите слово «Оператор».*"
        else:
            final_reply = bot_reply
    else:
        final_reply = bot_reply

    send_message_to_max(chat_id, final_reply)
    log_to_google_sheet(user_name, chat_id, text, bot_reply)
