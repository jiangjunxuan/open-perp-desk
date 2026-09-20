FROM python:3.12-slim

ARG API_PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --index-url "$API_PIP_INDEX_URL" -r requirements.txt

COPY app ./app

RUN mkdir -p /data && chown -R nobody:nogroup /data
USER nobody
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "20"]
