"""Keep unused endpoints out of the public REST surface."""

import pytest

from config.urls import api


@pytest.mark.parametrize(
    ("method", "path", "replacement"),
    [
        ("post", "/auth/register/", "/auth/login/"),
        ("get", "/organizations/", "/auth/me/"),
        ("get", "/plugins/{plugin_id}/", "/plugins/"),
        ("get", "/captured-images/{captured_image_id}/", "/captured-images/"),
        (
            "get",
            "/image-artifacts/deletions/{request_id}/",
            "/image-artifacts/deletions/",
        ),
        (
            "post",
            "/image-definitions/{definition_id}/deactivate/",
            "/image-artifacts/deletions/",
        ),
    ],
)
def test_unused_endpoints_are_not_exposed(
    method: str, path: str, replacement: str
) -> None:
    """Keep the current catalogue/profile and coordinated deletion APIs available."""
    paths = api.get_openapi_schema(path_prefix="")["paths"]
    assert method in paths[replacement]
    assert method not in paths.get(path, {})
