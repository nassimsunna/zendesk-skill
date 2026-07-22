"""Regression tests for the production prompt-security model cache."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_docker_preloads_security_model_into_runtime_cache():
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "FASTEMBED_CACHE_PATH=/app/.cache/fastembed" in dockerfile
    assert "HF_HOME=/app/.cache/huggingface" in dockerfile
    assert 'model_name="Qdrant/bge-small-en-v1.5-onnx-Q"' in dockerfile
    assert "next(model.embed" in dockerfile  # verifies model + tokenizer are usable
    assert "from zendesk_skill.render_entrypoint import create_app; create_app()" in dockerfile


def test_runtime_forbids_hugging_face_network_access():
    dockerfile = (ROOT / "Dockerfile").read_text()
    preload = dockerfile.index('model_name="Qdrant/bge-small-en-v1.5-onnx-Q"')
    offline = dockerfile.index("ENV HF_HUB_OFFLINE=1")

    # The build may download, but a fresh runtime container is forced to use
    # the image cache and cannot silently fall back to Hugging Face.
    assert offline > preload
    assert dockerfile.index("create_app; create_app()") > offline
