FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY main.py .

# Non-root user
RUN useradd -r -u 1000 -g root ingest
USER 1000

ENTRYPOINT ["python", "-u", "main.py"]
