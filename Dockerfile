# ---------- Stage 1: build dependencies + train model ----------
FROM python:3.11-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt
COPY app ./app
RUN PYTHONPATH=/install/lib/python3.11/site-packages python -m app.train

# ---------- Stage 2: minimal runtime image ----------
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 appuser
WORKDIR /srv
COPY --from=builder /install /usr/local
COPY --from=builder /build/app ./app
USER appuser
EXPOSE 5000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/health').status==200 else 1)"
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--access-logfile", "-", "app.wsgi:app"]