FROM python:3.12-slim

WORKDIR /app

RUN pip install uv --quiet

COPY . .

RUN uv sync --no-dev

EXPOSE 5003

ENV PYTHONUNBUFFERED=1

CMD ["uv", "run", "python", "ui/main.py"]
