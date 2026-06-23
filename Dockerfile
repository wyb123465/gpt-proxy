FROM python:3.12-slim

WORKDIR /app

ENV GPT_PROXY_CONFIG=/data/config.json \
    GPT_PROXY_STATE=/data/state.json

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main ./main
COPY static ./static
COPY config.example.json README.md ./

RUN mkdir -p /data

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
