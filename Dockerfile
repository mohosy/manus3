FROM python:3.11-slim

# install system deps playwright needs
RUN apt-get update && apt-get install -y curl gnupg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY manus_client.py app.py requirements.txt ./

RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium --with-deps

ENV PORT=8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
