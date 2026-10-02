import pytest

from knock.providers.vision.safety import (
    SAFE_FALLBACK_DESCRIPTION,
    contains_alarming_language,
    sanitize_description,
)


@pytest.mark.parametrize(
    "description",
    [
        "A person holding what could be a bomb",
        "Looks like they are carrying a weapon",
        "Possibly a gun visible near the doorway",
        "A knife is visible on the porch",
        "This could be a terrorist threat",
    ],
)
def test_contains_alarming_language_flags_speculative_danger(description: str) -> None:
    assert contains_alarming_language(description) is True


@pytest.mark.parametrize(
    "description",
    [
        "A person standing on the porch",
        "A cardboard box is visible near the door",
        "A delivery driver holding a package",
        "A vehicle is parked in the driveway",
    ],
)
def test_contains_alarming_language_allows_benign_descriptions(description: str) -> None:
    assert contains_alarming_language(description) is False


def test_sanitize_description_passes_through_benign_text() -> None:
    text = "A person holding a cardboard box"
    assert sanitize_description(text) == text


def test_sanitize_description_replaces_alarming_text_with_safe_fallback() -> None:
    assert sanitize_description("I see what might be a bomb") == SAFE_FALLBACK_DESCRIPTION


def test_contains_alarming_language_is_case_insensitive() -> None:
    assert contains_alarming_language("Possible BOMB visible") is True
