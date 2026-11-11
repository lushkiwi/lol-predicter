# The web UI as a container. Build it from the directory that `python -m draft_analyzer.deploy`
# assembles (the code plus the trained models and the data the server reads), not from a bare
# checkout: the repository does not contain the models.
FROM python:3.12-slim

# libgomp1: OpenMP runtime LightGBM needs.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*

# HOST: listen on every interface inside the container (the host sets PORT).
# OMP_NUM_THREADS: cores one prediction may use. Hosts often show a container every core of the
# machine; without a cap LightGBM would start a thread for each.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 OMP_NUM_THREADS=8

WORKDIR /app
COPY requirements-serve.txt .
RUN pip install -r requirements-serve.txt

COPY draft_analyzer ./draft_analyzer
COPY data ./data
COPY LICENSE MANIFEST.json ./

# Run unprivileged; the server caches champion images under data/raw/ddragon.
RUN useradd --create-home app && chown -R app /app/data
USER app

EXPOSE 8000
CMD ["python", "-m", "draft_analyzer.web"]
