"""jfollow9: destutter, stop+clear, one-card removal by position/name/kind, companion alias."""

from __future__ import annotations

from mac.voice import acks
from mac.voice.intents import (about_companion, card_matches, card_remove_request,
                               destutter, first_form, parse)

CARDS = [("c1", "info", ("ottoman", "empire", "milestones")),
         ("c2", "image", ("cat",)),
         ("c3", "video", ("moon", "landing", "footage")),
         ("c4", "info", ("notes", "on", "rome"))]


def _target(t: str) -> str | None:
    """The one-card name a phrase asks to remove: the parse's own target, else the ask."""
    p = parse(t, apps={})
    if p.kind == "card_remove":
        return p.sub
    ask = first_form(t, card_remove_request)
    return ask.target if ask else None


def test_stutter_and_combined_clear_parse():
    # A stuttered word that starts the next word's letters is dropped; an immediate
    # repeat keeps its second copy; clean text is untouched.
    assert destutter("clear the over, overlay") == "clear the overlay"
    assert destutter("remove the the cat card") == "remove the cat card"
    assert destutter("what time is it") == "what time is it"
    # "stop and clear" rides into the clear intent ("Okay, okay, " is a stripped lead).
    assert parse("Okay, okay, stop and clear everything", apps={}).kind == "clear"
    assert parse("stop and clear everything", apps={}).kind == "clear"
    # The wider remove-everything alternatives; the plain close-it/that/this stays clear.
    assert parse("remove everything", apps={}).kind == "clear"
    assert parse("close all the cards", apps={}).kind == "clear"
    assert parse("delete everything", apps={}).kind == "clear"
    assert parse("close it", apps={}).kind == "clear"
    assert parse("close that", apps={}).kind == "clear"
    assert parse("close this", apps={}).kind == "clear"


def test_card_words_parse():
    # Each phrase resolves to exactly one card, by position, title (with the STT
    # plural fold and possessive strip) or kind.
    wanted = [("remove the first card", "c1"), ("remove the second card", "c2"),
              ("remove the third card", "c3"), ("remove card number two", "c2"),
              ("remove the card about cats", "c2"), ("remove the cats card", "c2"),
              ("remove the Cat's card", "c2"), ("delete the cat card", "c2"),
              ("remove the the cat card", "c2"), ("get rid of the cat picture", "c2"),
              ("remove the card about the Ottoman empire", "c1"),
              ("close the video", "c3"), ("close the clip", "c3")]
    for t, cid in wanted:
        m = card_matches(_target(t), CARDS)
        assert m is not None and len(m[1]) == 1 and m[1][0] == cid, (t, m)
    for t in ("remove the fifth card", "remove the dog card"):
        assert card_matches(_target(t), CARDS) is None, t
    # Two cards sharing the spoken word: both come back, not one at random.
    two = [("a1", "info", ("rome", "history")), ("a2", "info", ("rome", "facts"))]
    m = card_matches(_target("remove the rome card"), two)
    assert m is not None and len(m[1]) == 2, m
    # A spoken kind with one card of that kind: that card.
    m = card_matches(_target("remove the info card"), [("b1", "info", ("rome", "history"))])
    assert m == ("kind", ["b1"]), m


def test_soft_kind_words_keep_parse():
    # The new kind words are too weak to turn "close the notes" into a card remove:
    # these parsed "answer" before jfollow9 and still do (measured on the base file).
    assert parse("close the notes", apps={}).kind == "answer"
    assert parse("remove the link", apps={}).kind == "answer"
    assert parse("close the clip", apps={}).kind == "answer"


def test_about_companion_aliases():
    for t in ("check Jarvis Companion capabilities",
              "Can you show me the capabilities of Java's companion for other users",
              "companion", "I meant the Jarvis companion", "what can the Jarvis company do",
              "tell me about the companion app", "Jarvis companions features"):
        assert about_companion(t), t
    for t in ("what can you do", "what are my sessions doing on my companion",
              "the companion one", "remove the companion capabilities card",
              "show me a card titled Jarvis capabilities"):
        assert not about_companion(t), t
    # The companion question itself still parses as a plain answer.
    assert parse("what can Jarvis Companion do", apps={}).kind == "answer"
    # The new ack keys exist, the answer line within the ack length cap.
    for key in ("removed_named", "no_card_named", "no_cards", "companion_about"):
        assert key in acks.PHRASES, key
    assert len(acks.PHRASES["companion_about"].split()) <= 12
