"""Regression tests for the production prompt-security model cache."""

from pathlib import Path

from fastembed import TextEmbedding


ROOT = Path(__file__).resolve().parents[1]
MODEL_NAME = "BAAI/bge-small-en-v1.5"


def test_docker_preloads_security_model_into_runtime_cache():
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "FASTEMBED_CACHE_PATH=/app/.cache/fastembed" in dockerfile
    assert "HF_HOME=/app/.cache/huggingface" in dockerfile
    assert "HUGGINGFACE_HUB_CACHE=/app/.cache/huggingface/hub" in dockerfile
    assert f'model_name="{MODEL_NAME}"' in dockerfile
    assert "next(model.embed" in dockerfile  # verifies model + tokenizer are usable
    assert "from zendesk_skill.render_entrypoint import create_app; create_app()" in dockerfile


def test_docker_preload_model_is_supported_by_fastembed():
    supported_models = {
        model["model"] for model in TextEmbedding.list_supported_models()
    }

    assert MODEL_NAME in supported_models


def test_docker_verifies_preloaded_model_without_network_access():
    dockerfile = (ROOT / "Dockerfile").read_text()
    offline_verification = dockerfile.index("RUN --network=none HF_HUB_OFFLINE=1")

    assert dockerfile.index(f'model_name="{MODEL_NAME}"', offline_verification)
    assert dockerfile.index(
        'cache_dir=os.environ["FASTEMBED_CACHE_PATH"]', offline_verification
    )
    assert dockerfile.index("local_files_only=True", offline_verification)
    assert dockerfile.index("next(model.embed", offline_verification)


def test_runtime_forbids_hugging_face_network_access():
    dockerfile = (ROOT / "Dockerfile").read_text()
    preload = dockerfile.index(f'model_name="{MODEL_NAME}"')
    offline = dockerfile.index("ENV HF_HUB_OFFLINE=1")

    # The build may download, but a fresh runtime container is forced to use
    # the image cache and cannot silently fall back to Hugging Face.
    assert offline > preload
    assert dockerfile.index("create_app; create_app()") > offline
