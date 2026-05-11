FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dashboard.py .
COPY announcements.json .
EXPOSE 8080
CMD ["gunicorn", "dashboard:app", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "120"]
