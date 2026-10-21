FROM eclipse-temurin:21-jre AS java
FROM python:3.12-slim AS build
COPY --from=java /opt/java/openjdk /opt/java/openjdk
ENV JAVA=/opt/java/openjdk/bin/java
WORKDIR /project
COPY scripts/build.py scripts/build.py
COPY templates templates
COPY openapi openapi
RUN python scripts/build.py

FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=build /project/build/generated/src /app/generated
COPY app app
COPY openapi openapi
ENV PYTHONPATH=/app/generated PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --create-home marketplace
USER marketplace
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=2)"
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
