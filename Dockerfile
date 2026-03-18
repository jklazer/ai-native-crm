FROM python:3.11-slim

# Рабочая директория
WORKDIR /app

# Копируем зависимости первыми для кеширования слоёв Docker
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY . .

# Запуск как модуль Python (поддерживает относительные импорты)
CMD ["python", "-m", "ai_native_crm.main"]
