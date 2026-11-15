"""Owner labels never reveal an email address or authentication identifier."""

from types import SimpleNamespace

import pytest

from common.user_labels import owner_label


@pytest.mark.parametrize(
    "name,expected",
    [(" Ada Lovelace ", "Ada Lovelace"), ("", "User 42"), ("a@b.test", "User 42")],
)
def test_owner_labels_preserve_privacy(name: str, expected: str) -> None:
    """Use the existing display name or an opaque owner ID fallback."""
    owner = SimpleNamespace(pk=42, get_full_name=lambda: name)
    assert owner_label(owner) == expected
    assert owner_label(None) == "Unknown owner"
