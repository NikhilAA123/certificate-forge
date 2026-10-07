FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY src ./src
RUN pip install --no-cache-dir -c requirements.lock . && useradd --create-home appuser && mkdir /app/data && chown appuser /app/data
USER appuser
ENV CERTIFICATE_DATA_DIR=/app/data
EXPOSE 8000
CMD ["python", "-m", "certificate_forge", "--host", "0.0.0.0"]
