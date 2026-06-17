# FSKX Runner image.
#
# Strategy: a small base environment (Python + Flask) runs the web server. Each FSKX
# model gets its OWN micromamba environment, created on first run from the model's
# packages.json + scanned imports (R or Python, with the requested version where
# available). Those per-model environments live under /opt/conda/envs, which the
# launchers mount as a persistent named volume so they are built once and reused.
#
# This keeps the image generic: no model-specific dependencies are baked in, so it
# works for any R or Python FSKX model, not just the bundled examples.

FROM mambaorg/micromamba:1.5.10

# Base env: the web server + HTTP client (for the repository and the Claude API).
RUN micromamba install -y -n base -c conda-forge \
        python=3.11 flask=3.* requests curl tar && \
    micromamba clean --all --yes

# Docker CLI (client only) so the app can build/run per-model images against the host
# daemon via the mounted socket. Arch-aware static binary; no daemon is installed.
# Needs root to write into /usr/local/bin (the base image runs as mambauser).
USER root
ARG TARGETARCH
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64) DARCH=x86_64 ;; \
      arm64) DARCH=aarch64 ;; \
      *) DARCH=x86_64 ;; \
    esac; \
    /opt/conda/bin/curl -fsSL "https://download.docker.com/linux/static/stable/${DARCH}/docker-27.3.1.tgz" -o /tmp/docker.tgz; \
    tar -xzf /tmp/docker.tgz -C /tmp; \
    cp /tmp/docker/docker /usr/local/bin/docker; \
    chmod +x /usr/local/bin/docker; \
    rm -rf /tmp/docker /tmp/docker.tgz

# micromamba activates the base env for RUN/CMD via this flag.
ARG MAMBA_DOCKERFILE_ACTIVATE=1

ENV MAMBA_ROOT_PREFIX=/opt/conda \
    FSKX_MODELS_DIR=/models \
    FSKX_WORK_DIR=/work \
    FSKX_ENV_LOGS=/work/envlogs \
    FSKX_WORK_VOLUME=fskx_work \
    PORT=8000 \
    MPLBACKEND=Agg

COPY --chown=$MAMBA_USER:$MAMBA_USER app/ /app/

# Run as root so the server can always write to /work and /opt/conda/envs — including
# Docker named volumes, which are created root-owned. This keeps the launcher simple
# (no volume pre-provisioning) for a local single-user tool.
USER root
RUN mkdir -p /work /models /opt/conda/envs

WORKDIR /app
EXPOSE 8000

# _entrypoint.sh (from the base image) activates the base env, then runs the server.
CMD ["python", "server.py"]
