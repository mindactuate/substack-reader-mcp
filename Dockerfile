FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    SUBSTACK_CACHE_FILE=/data/cache.json \
    SUBSTACK_STATS_FILE=off

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY substack_mcp.py .
COPY prompts/ prompts/

# No root in the container; /data is the mount point for the cache bucket
RUN useradd --uid 10001 --no-create-home app && mkdir -p /data && chown app /data
USER app

# SUBSTACK_SID and MCP_SECRET come from Secret Manager at runtime, Cloud Run sets PORT
CMD ["python", "substack_mcp.py"]
