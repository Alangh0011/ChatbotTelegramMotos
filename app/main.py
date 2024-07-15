from dotenv import load_dotenv
load_dotenv()
import json
import random
import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext, Dispatcher
from azure.storage.blob import BlobServiceClient
from azure.cosmos import exceptions, CosmosClient, PartitionKey
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, time as dtime
import pytz
from flask import Flask, request

app = Flask(__name__)

# Configura tu token de Telegram
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')

# Configura tu conexión de Azure
AZURE_CONNECTION_STRING = os.getenv('AZURE_CONNECTION_STRING')
BLOB_CONTAINER_NAME = 'banco'
BLOB_NAME = 'banco.json'

# Configuración de Cosmos DB
COSMOS_DB_URI = os.getenv('COSMOS_DB_URI')
COSMOS_DB_KEY = os.getenv('COSMOS_DB_KEY')
DATABASE_NAME = 'TelegramBotDB'
CONTAINER_NAME = 'ChatIDs'

# Contraseña para autenticación
BOT_PASSWORD = 'javi'

# Crear cliente de Cosmos DB
client = CosmosClient(COSMOS_DB_URI, COSMOS_DB_KEY)
database = client.create_database_if_not_exists(id=DATABASE_NAME)
container = database.create_container_if_not_exists(
    id=CONTAINER_NAME,
    partition_key=PartitionKey(path="/chat_id"),
    offer_throughput=400
)

def add_chat_id(chat_id):
    try:
        container.create_item(body={"id": str(chat_id), "chat_id": str(chat_id)})
    except exceptions.CosmosResourceExistsError:
        logging.info(f"Chat ID {chat_id} ya existe en Cosmos DB")

def get_chat_ids():
    query = "SELECT c.chat_id FROM c"
    items = list(container.query_items(query, enable_cross_partition_query=True))
    return [item['chat_id'] for item in items]

def is_authenticated(chat_id):
    query = f"SELECT c.chat_id FROM c WHERE c.chat_id = '{chat_id}'"
    items = list(container.query_items(query, enable_cross_partition_query=True))
    return len(items) > 0

def load_questions():
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
        blob_client = blob_service_client.get_blob_client(container=BLOB_CONTAINER_NAME, blob=BLOB_NAME)
        blob_data = blob_client.download_blob().readall()
        questions = json.loads(blob_data)
        logging.info("Preguntas cargadas correctamente desde Azure Blob Storage")
        return questions
    except Exception as e:
        logging.error(f"Error cargando preguntas: {e}")
        return []

def split_text(text, max_length):
    words = text.split(' ')
    result = []
    current_line = ""

    for word in words:
        if len(current_line) + len(word) + 1 > max_length:
            result.append(current_line)
            current_line = word
        else:
            if current_line:
                current_line += ' ' + word
            else:
                current_line = word

    if current_line:
        result.append(current_line)

    return '\n'.join(result)

def format_options(options, max_length):
    formatted_options = []
    for option in options:
        split_option = split_text(option, max_length)
        formatted_options.append(split_option)
    return formatted_options

def send_question(context: CallbackContext, chat_id: int):
    questions = load_questions()
    if not questions:
        logging.error("No se pudieron cargar las preguntas")
        return
    
    question = random.choice(questions)
    if 'questions' not in context.bot_data:
        context.bot_data['questions'] = {}
    context.bot_data['questions'][str(chat_id)] = question

    max_length = 40  # Ajusta este valor según sea necesario
    formatted_options = format_options(question['options'], max_length)
    keyboard = [
        [InlineKeyboardButton(option, callback_data=option[0])] for option in formatted_options
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        context.bot.send_message(chat_id=chat_id, text=question['question'], reply_markup=reply_markup)
        logging.info(f"Pregunta enviada al chat ID {chat_id}")
    except Exception as e:
        logging.error(f"Error al enviar la pregunta: {e}")

def start(update: Update, context: CallbackContext) -> None:
    chat_id = update.message.chat_id
    logging.info(f'Chat ID: {chat_id}')  # Logging del chat ID

    if is_authenticated(chat_id):
        context.user_data['authenticated'] = True
        show_menu(update, context)
    else:
        context.user_data['authenticated'] = False
        update.message.reply_text('Por favor, ingrese la contraseña para continuar:')

def show_menu(update: Update, context: CallbackContext):
    keyboard = [
        [KeyboardButton("Información"), KeyboardButton("Iniciar"), KeyboardButton("Instrucciones")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    update.message.reply_text('Elige una opción:', reply_markup=reply_markup)

def button(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()
    chat_id = str(query.message.chat_id)
    try:
        question = context.bot_data['questions'][chat_id]
        selected_option = query.data
        if selected_option == question['correct']:
            query.edit_message_text(text="¡Correcto! Lo estas haciendo bien 🥳🥳")
        else:
            feedback = question['feedback']
            query.edit_message_text(text=f"Incorrecto. {feedback['text']}")
            if 'image' in feedback:
                try:
                    query.message.reply_photo(feedback['image'])
                except Exception as e:
                    logging.error(f"Error al enviar la imagen: {e}")
                    query.message.reply_text("No hay imagen que mostrar.")
            if 'video' in feedback:
                try:
                    video_url = feedback['video']
                    if video_url.startswith("https://"):
                        query.message.reply_video(video=video_url)
                    else:
                        raise ValueError("URL del video no válida.")
                except Exception as e:
                    logging.error(f"Error al enviar el video: {e}")
                    query.message.reply_text("No se pudo cargar el video.")
    except KeyError as e:
        query.edit_message_text(text="Error: No se encontró la pregunta actual.")
        logging.error(f"Error: {e}. Context Bot Data: {context.bot_data}")

def handle_message(update: Update, context: CallbackContext) -> None:
    text = update.message.text
    chat_id = update.message.chat_id

    if not context.user_data.get('authenticated', False):
        if text == BOT_PASSWORD:
            context.user_data['authenticated'] = True
            add_chat_id(chat_id)
            show_menu(update, context)
        else:
            update.message.reply_text('🔒 Contraseña incorrecta. Inténtalo de nuevo.')
        return

    if text == "Información":
        update.message.reply_text(
            "¡Hola! Soy Javi, tu asistente de aprendizaje. 🧑‍🏫📚\n\n"
            "Mi propósito es ayudarte a optimizar y perfeccionar los procedimientos que aplicas en tu trabajo, en el área de motos "
            "a través de preguntas y respuestas. 📈💡\n\n"
            "Fue creado para que aprendas de forma interactiva y con cariño del área de aprendizaje Suburbia y "
            "diseño de experiencias. 🤖❤️\n\n"
            "Creador: Este bot ha sido desarrollado con cariño y dedicación por Alan Gomez, para facilitar tu "
            "aprendizaje continuo y mejorar tus habilidades. 🐶🏆🧑‍💻"
        )
    elif text == "Iniciar":
        send_question(context, chat_id)
    elif text == "Instrucciones":
        update.message.reply_text(
            "Instrucciones para usar el bot Javi:\n\n"
            "1. Menú principal:\n"
            "   - Información: Obtén información sobre el bot.\n"
            "   - Iniciar: Recibe una nueva pregunta de aprendizaje.\n"
            "   - Instrucciones: Consulta cómo usar el bot.\n\n"
            "2. Funcionamiento:\n"
            "   - Recibirás preguntas periódicamente a las 9 AM, 12 PM, 3 PM y 6 PM.\n"
            "   - Puedes solicitar una nueva pregunta en cualquier momento seleccionando 'Iniciar' en el menú.\n"
            "   - No se responderán mensajes de texto fuera de las opciones proporcionadas.\n\n"
            "3. Retroalimentación:\n"
            "   - Si tu respuesta es incorrecta, recibirás material de repaso como infografías, audios, videos y descripciones.\n"
            "   - No te preocupes por equivocarte, ¡aprendemos de nuestros errores!\n\n"
            "¡Encantado de ayudarte a aprender y mejorar! 🐶🏆🧑‍💻"
        )
    else:
        show_menu(update, context)

def help_command(update: Update, context: CallbackContext) -> None:
    help_text = (
        "¡Hola! Soy Javi, tu asistente de aprendizaje interactivo. 🧑‍🏫📚\n\n"
        "Aquí tienes una guía rápida sobre cómo utilizarme:\n\n"
        "/start - Inicia la interacción con el bot y te despliega el siguiente menú.\n"
        "Información - Te daré una breve descripción sobre mí.\n"
        "Iniciar - Envía una nueva pregunta para que la respondas.\n"
        "Instrucciones - Consulta cómo usar el bot y los tiempos establecidos para recibir preguntas.\n\n"
        "Recuerda que este bot está diseñado para que respondas preguntas y recibas retroalimentación inmediata. "
        "No responderé a mensajes de texto fuera de las opciones proporcionadas. 📈💡\n\n"
        "¡Encantado de ayudarte a aprender y mejorar! 🐶🏆🧑‍💻"
    )
    update.message.reply_text(help_text)

def scheduled_question(context: CallbackContext):
    chat_ids = get_chat_ids()
    for chat_id in chat_ids:
        logging.info(f"Enviando pregunta al chat_id: {chat_id}")  # Logging para seguimiento
        send_question(context, chat_id)
        time.sleep(1)  # Esperar 1 segundo antes de enviar la siguiente pregunta

def schedule_jobs():
    scheduler = BackgroundScheduler()
    timezone = pytz.timezone('America/Mexico_City')  # Ajusta esto según tu zona horaria
    times = ["09:00", "12:00", "15:00", "18:00"]
    for time_str in times:
        hour, minute = map(int, time_str.split(':'))
        scheduler.add_job(scheduled_question, 'cron', hour=hour, minute=minute, timezone=timezone)
    scheduler.start()

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler('start', start))
    dispatcher.add_handler(CommandHandler('help', help_command))
    dispatcher.add_handler(CallbackQueryHandler(button))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    update = Update.de_json(data, updater.bot)
    dispatcher.process_update(update)
    return "ok", 200

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    schedule_jobs()
    app.run(debug=True, use_reloader=False)
