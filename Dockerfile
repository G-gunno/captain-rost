FROM python:3.11-slim

WORKDIR /app

# Копируем список библиотек и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь остальной код
COPY . .

# Команда для запуска бота
CMD ["python", "main.py"]
