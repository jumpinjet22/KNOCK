import pytest

from knock.core.outbound_text import (
    CALL_MAX_CHARS,
    PUSH_MAX_CHARS,
    UNTRUSTED_CONTACT_NOTE,
    for_call,
    for_push,
    has_contact,
    one_line,
)

# Wording KNOCK itself produces today -- must come out of both channels unchanged.
ORDINARY = [
    "A food delivery is at the door (camera: cam1).",
    "A delivery at the door needs a signature (camera: 696ab4810067bb03e400a1fc).",
    "UPS package for Jon (tracking ...6784)",
    "A package was left at the door (door: Front door).",
    'Possible emergency at the door: "help, fire!"',
    "Unsure what a visitor at the door wants (camera: cam1). (best guesses: delivery, visitation)",
    "A FedEx driver has a package for you",
]


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_notifications_are_untouched(text) -> None:
    assert for_push(text) == text
    assert for_call(text) == text
    assert not has_contact(text)


# -- one_line ---------------------------------------------------------------------------------


def test_one_line_collapses_line_breaks_tabs_and_runs_of_spaces() -> None:
    assert one_line("a\nb\r\n\tc   d", 100) == "a b c d"


def test_one_line_removes_invisible_and_bidi_control_characters() -> None:
    assert one_line("ig​nore ‮evil‬ ﻿end", 100) == "ignore evil end"


def test_one_line_cuts_to_the_limit_with_an_ellipsis() -> None:
    cut = one_line("word " * 100, 20)

    assert len(cut) <= 20 and cut.endswith("...")
    assert one_line("short", 20) == "short"
    assert one_line("", 20) == ""


# -- has_contact ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "call 555-0100",
        "call (555) 010-0100 now",
        "call +1 555 010 0100",
        "dial 5550100",
        "http://evil.example/x",
        "https://evil.example",
        "go to www.evil.example",
        "HTTP://EVIL.EXAMPLE",
    ],
)
def test_links_and_phone_numbers_are_detected(text) -> None:
    assert has_contact(text)


@pytest.mark.parametrize(
    "text",
    [
        "tracking ...6784",
        "camera: 66eca36a03694503e4000ccf",
        "6 pm tomorrow",
        "apartment 4B",
        "ref ZQ3248",
        "1Z999AA10123456784",
    ],
)
def test_ids_and_short_numbers_are_not_mistaken_for_contacts(text) -> None:
    assert not has_contact(text)


# -- for_push ---------------------------------------------------------------------------------


def test_push_keeps_a_link_or_number_but_flags_it_once() -> None:
    text = for_push("Urgent: bank called, phone 555-0100 immediately")

    assert "555-0100" in text
    assert text.endswith(UNTRUSTED_CONTACT_NOTE)
    assert text.count(UNTRUSTED_CONTACT_NOTE) == 1


def test_push_is_one_bounded_line_even_when_flagged() -> None:
    long = "Urgent " * 100 + "call 555-0100"

    for text in (for_push(long), for_push("x\n" * 500)):
        assert "\n" not in text
        assert len(text) <= PUSH_MAX_CHARS


def test_a_number_cut_off_by_the_limit_still_triggers_the_flag() -> None:
    text = for_push("filler " * 60 + "call 555-0100")

    assert text.endswith(UNTRUSTED_CONTACT_NOTE)
    assert "555-0100" not in text.removesuffix(UNTRUSTED_CONTACT_NOTE)


# -- for_call ---------------------------------------------------------------------------------


def test_a_call_never_reads_a_link_or_number_aloud() -> None:
    spoken = for_call("Open http://evil.example/x or www.evil.example and call (555) 010-0100")

    assert "evil" not in spoken and "555" not in spoken
    assert "a link" in spoken and "a phone number" in spoken


def test_a_call_is_one_bounded_line() -> None:
    spoken = for_call("alarm\n" * 200)

    assert "\n" not in spoken
    assert len(spoken) <= CALL_MAX_CHARS
