FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=http \
    HF_HOME=/app/.cache/huggingface \
    HUGGINGFACE_HUB_CACHE=/app/.cache/huggingface/hub \
    FASTEMBED_CACHE_PATH=/app/.cache/fastembed \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# prompt-security-utils uses this FastEmbed model when screening untrusted
# Zendesk content. Materialize both the ONNX model and tokenizer artifacts in a
# stable image layer, then make Hugging Face strictly offline at runtime.
RUN mkdir -p "$HF_HOME" "$HUGGINGFACE_HUB_CACHE" "$FASTEMBED_CACHE_PATH" && \
    python -c 'import os; from fastembed import TextEmbedding; model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5", cache_dir=os.environ["FASTEMBED_CACHE_PATH"]); next(model.embed(["build-time cache verification"]))'
# Prove that the cached model is complete without allowing a network fallback.
RUN --network=none HF_HUB_OFFLINE=1 python -c 'import os; from fastembed import TextEmbedding; model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5", cache_dir=os.environ["FASTEMBED_CACHE_PATH"], local_files_only=True); next(model.embed(["offline cache verification"]))'
ENV HF_HUB_OFFLINE=1
# Fail the image build unless the production application can initialize while
# Hugging Face is offline and the preloaded artifacts are complete.
RUN python -c 'from zendesk_skill.render_entrypoint import create_app; create_app()'

EXPOSE 8000
CMD ["python", "-m", "zendesk_skill.render_entrypoint"]
