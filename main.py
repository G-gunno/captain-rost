import os
import asyncio
from loguru import logger
from telegram.ext import Application, CommandHandler

# --- Обработчики команд Telegram ---
async def cmd_start(update, context):
    await update.message.reply_text("🤖 Капитан Рост на связи! Бот успешно запущен на сервере Render.")

async def cmd_status(update, context):
    # Пока это заглушка, позже здесь будет баланс и статистика
    await update.message.reply_text("📊 Статус: Тестируем подключение. Балансы пока не подключены.")

# --- Главная функция ---
def main():
    logger.info("Запуск бота CaptainRost...")
    
    # Токен мы будем брать из секретных настроек Render
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    
    if not token:
        logger.error("Ошибка! TELEGRAM_BOT_TOKEN не найден в настройках сервера.")
        return

    # Создаем приложение Telegram
    app = Application.builder().token(token).build()
    
    # Привязываем команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))

    logger.info("Бот успешно стартовал и слушает Telegram...")
    # Запускаем бесконечный цикл ожидания сообщений
    app.run_polling()

if __name__ == '__main__':
    main()
