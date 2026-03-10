FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8765

CMD ["python3", "-m", "uvicorn", "web.app:app", "--app-dir", "scripts", "--host", "0.0.0.0", "--port", "8765"]
