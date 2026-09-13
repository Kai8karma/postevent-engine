FROM python:3.12-slim

WORKDIR /app
COPY . .

# Stdlib only -- no pip install step, matching this repo's no-pip-deps rule.
EXPOSE 8080
CMD ["python3", "api/server.py"]
