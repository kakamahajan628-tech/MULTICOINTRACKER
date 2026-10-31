FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8002 WHALE_HOST=0.0.0.0
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml README.md NOTICE.md ./
COPY licenses ./licenses
COPY whale_watcher ./whale_watcher
COPY liquidity ./liquidity
RUN pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home appuser \
    && mkdir /app/runtime && chown appuser:appuser /app/runtime
USER appuser
EXPOSE 8002
HEALTHCHECK --interval=30s --start-period=45s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/health',timeout=3)"
CMD ["python","-m","whale_watcher"]

