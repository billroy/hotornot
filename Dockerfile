FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py wsgi.py ./
COPY templates ./templates
COPY static ./static

EXPOSE 8080

CMD ["gunicorn", "--workers", "1", "--threads", "20", "--worker-class", "gthread", "--bind", "0.0.0.0:8080", "--access-logfile", "-", "--error-logfile", "-", "wsgi:application"]
