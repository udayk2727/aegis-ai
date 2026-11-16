# Local demo image for the FastAPI backend. Build from the repository root, which
# is the context set in compose.yaml. It holds only the core runtime: the optional
# LLM SDKs, the analytics packages, the tests, and local manifests stay out.
#
# Development-only settings (the simulation and demo routes, the trusted-baseline
# manifest) are not set here. compose.yaml sets them for the demo alone.

FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install -r requirements.txt

COPY backend/app ./backend/app
COPY scripts/approve_baselines.py scripts/load_demo_seeds.py ./scripts/

# An unprivileged user owns /data, the volume that holds the baseline manifest.
RUN useradd --system --uid 10001 --no-create-home aegis \
    && mkdir /data \
    && chown aegis:aegis /data
USER aegis

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--app-dir", "backend", \
     "--host", "0.0.0.0", "--port", "8000"]
