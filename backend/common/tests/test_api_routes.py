"""Keep redundant catalogue reads out of the public REST surface."""

import pytest

from config.urls import api


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        ("/organizations/", "/auth/me/"),
        ("/plugins/{plugin_id}/", "/plugins/"),
        ("/captured-images/{captured_image_id}/", "/captured-images/"),
        ("/image-artifacts/deletions/{request_id}/", "/image-artifacts/deletions/"),
    ],
)
def test_redundant_catalogue_reads_are_not_exposed(path: str, replacement: str) -> None:
    """Clients read these records through their existing catalogue/profile APIs."""
    paths = api.get_openapi_schema(path_prefix="")["paths"]
    assert "get" in paths[replacement]
    assert "get" not in paths.get(path, {})
