FROM python:3.11-slim
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home appuser
COPY app ./app
COPY scripts ./scripts
RUN mkdir -p /data /models/fastembed /app/secrets && chown -R appuser:appuser /data /models
USER appuser
ENV DATA_DIR=/data
EXPOSE 8080
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
