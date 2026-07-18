import pytest

from zendesk_skill.render_entrypoint import public_netloc_from_base_url


def test_public_netloc_accepts_render_origin():
    assert public_netloc_from_base_url("https://zendesk-talk-mcp.onrender.com") == "zendesk-talk-mcp.onrender.com"
    assert public_netloc_from_base_url("https://zendesk-talk-mcp.onrender.com/") == "zendesk-talk-mcp.onrender.com"


def test_public_netloc_allows_explicit_port():
    assert public_netloc_from_base_url("https://mcp.example.com:8443") == "mcp.example.com:8443"


@pytest.mark.parametrize(
    "value",
    [
        "zendesk-talk-mcp.onrender.com",
        "ftp://zendesk-talk-mcp.onrender.com",
        "https://user:secret@zendesk-talk-mcp.onrender.com",
        "https://zendesk-talk-mcp.onrender.com/mcp",
        "https://zendesk-talk-mcp.onrender.com?debug=1",
        "https://zendesk-talk-mcp.onrender.com#fragment",
    ],
)
def test_public_netloc_rejects_unsafe_values(value):
    with pytest.raises(ValueError):
        public_netloc_from_base_url(value)


def test_public_netloc_allows_missing_optional_value():
    assert public_netloc_from_base_url(None) is None
    assert public_netloc_from_base_url("") is None
