FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py index.html manifest.webmanifest ./
COPY icons ./icons
COPY rag_files ./rag_files

EXPOSE 8000
CMD ["python", "main.py"]
