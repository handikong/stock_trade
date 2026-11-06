FROM python:3.12-slim
WORKDIR /app
COPY research /app/research
COPY config /app/config
COPY gateway/static /app/gateway/static
RUN pip install --no-cache-dir -e /app/research
ENV DATA_DIR=/app/data
EXPOSE 8000
CMD ["uvicorn", "stock_research.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
