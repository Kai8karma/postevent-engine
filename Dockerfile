FROM python:3.12-slim

# ffmpeg: M3's transcribe phase (mono/16kHz audio extraction) and clip
# cutting need a real ffmpeg binary in the container -- no Python package
# substitutes for it. --no-install-recommends + apt list cleanup keeps the
# image small (matches this repo's no-pip-deps, stdlib-only rule for the
# Python side).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# Stdlib only -- no pip install step, matching this repo's no-pip-deps rule.
EXPOSE 8080
CMD ["python3", "api/server.py"]
