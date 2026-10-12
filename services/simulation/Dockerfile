FROM python:3.14-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY services/simulation/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY shared /app/shared
COPY services/__init__.py /app/services/__init__.py
COPY services/simulation /app/services/simulation
CMD ["python", "-m", "services.simulation.main"]
