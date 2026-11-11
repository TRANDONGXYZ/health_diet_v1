FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_MODE=hosted PORT=10000
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ingredients.json ingredient_names_vi.json ./
COPY mealapp ./mealapp
COPY public ./public
RUN useradd --create-home --uid 10001 mealapp
USER mealapp
EXPOSE 10000
CMD ["python", "app.py"]
