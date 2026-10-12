FROM python:3.14-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN pip install --no-cache-dir pika==1.3.2
COPY shared /app/shared
COPY services/__init__.py /app/services/__init__.py
COPY services/alerts /app/services/alerts
CMD ["python", "-m", "services.alerts.main"]
