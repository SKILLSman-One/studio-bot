ARG PYTHON_VERSION=3.13
FROM python:${PYTHON_VERSION}-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 studio \
    && useradd --uid 10001 --gid studio --no-create-home studio \
    && mkdir /app/data && chown studio:studio /app/data
COPY studio_bot ./studio_bot
COPY bootstrap.py .
COPY assets ./assets
COPY studio.toml .
USER 10001:10001
ENTRYPOINT ["python", "bootstrap.py"]
CMD ["run"]
