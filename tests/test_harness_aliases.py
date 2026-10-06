"""Tests for omnigent.harness_aliases."""

from __future__ import annotations

import pytest

import omnigent.harness_aliases as harness_aliases
from omnigent.harness_aliases import (
    canonicalize_harness,
    is_native_harness,
    native_terminal_name,
)
from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES


@pytest.mark.parametrize(
    "alias,canonical",
    [
        ("claude", "claude-sdk"),
        ("native-pi", "pi-native"),
        ("native-kiro", "kiro-native"),
        # Docs / runtime-dispatch spelling of the openai-agents harness;
        # specs and OMNIGENT_HARNESSES use "openai-agents".
        ("openai-agents-sdk", "openai-agents"),
        # Canonical names pass through unchanged.
        ("openai-agents", "openai-agents"),
        ("pi", "pi"),
        ("prime-native", "prime-native"),
        # Canonical cursor id passes through unchanged (no alias).
        ("cursor", "cursor"),
        # Antigravity SDK harness: user-facing spellings → canonical id.
        ("agy", "antigravity"),
        ("google-antigravity", "antigravity"),
        ("antigravity", "antigravity"),
        # Antigravity native harness aliases.
        ("agy-native", "antigravity-native"),
        ("native-agy", "antigravity-native"),
        ("native-antigravity", "antigravity-native"),
        # Unknown names return unchanged so callers keep their own errors.
        ("bogus", "bogus"),
        (None, None),
    ],
)
def test_canonicalize_harness(alias: str | None, canonical: str | None) -> None:
    """Alias spellings map to canonical ids; everything else passes through.

    A missing ``openai-agents-sdk`` mapping breaks the documented
    ``omnigent run ... --harness openai-agents-sdk`` invocation at
    ``_validate_harness``.
    """
    assert canonicalize_harness(alias) == canonical


@pytest.mark.parametrize(
    "harness,expected",
    [
        # Canonical native spellings and their reversed forms.
        ("claude-native", True),
        ("codex-native", True),
        ("native-claude", True),
        ("native-codex", True),
        ("pi-native", True),
        ("native-pi", True),
        ("prime-native", True),
        ("kiro-native", True),
        ("native-kiro", True),
        ("antigravity-native", True),
        ("native-antigravity", True),
        ("agy-native", True),
        ("native-agy", True),
        # SDK harnesses are NOT native — they replay the Omnigent
        # transcript and don't own an on-disk runtime transcript. A
        # regression that classified these as native would wrongly route a
        # fork into the native-rebuild path.
        ("claude-sdk", False),
        ("claude_sdk", False),
        ("openai-agents", False),
        ("agents_sdk", False),
        ("codex", False),
        ("kiro", False),
        ("antigravity", False),
        ("agy", False),
        # The "claude" shorthand canonicalizes to claude-sdk (not native).
        ("claude", False),
        # cursor is a headless ACP harness, not a native CLI bridge.
        ("cursor", False),
        ("some-unknown-harness", False),
        ("native-prime", False),
        (None, False),
    ],
)
def test_is_native_harness(harness: str | None, expected: bool) -> None:
    """``is_native_harness`` flags only the native CLI harnesses.

    The runner gates terminal-owned turn sequencing and history replay on
    this. Misclassifying either way makes native TUI sessions behave like
    in-process SDK turns, or vice versa.
    """
    assert is_native_harness(harness) is expected


def test_kiro_native_is_valid_omnigent_harness_but_plain_kiro_is_not() -> None:
    """Kiro's native identity is canonical; plain ``kiro`` is not a generic harness."""
    assert "kiro-native" in OMNIGENT_HARNESSES
    assert "kiro" not in OMNIGENT_HARNESSES


@pytest.mark.parametrize(
    ("harness", "expected"),
    [
        ("claude-native", "claude"),
        ("native-claude", "claude"),  # reversed alias
        ("codex-native", "codex"),
        ("native-codex", "codex"),
        ("cursor-native", "cursor"),
        ("native-cursor", "cursor"),
        ("devin-native", "devin"),
        ("native-devin", "devin"),
        ("devin", "devin"),
        ("devin-acp", "devin"),
        ("goose-native", "goose"),
        ("native-goose", "goose"),
        ("hermes-native", "hermes"),
        ("native-hermes", "hermes"),
        ("kiro-native", "kiro"),
        ("native-kiro", "kiro"),
        ("qwen-native", "qwen"),
        ("native-qwen", "qwen"),
        ("kimi-native", "kimi"),
        ("native-kimi", "kimi"),
        ("pi-native", "pi"),
        ("native-pi", "pi"),
        ("prime-native", "prime-native"),
        ("antigravity-native", "antigravity"),
        ("native-antigravity", "antigravity"),
        ("agy-native", "antigravity"),
        ("native-agy", "antigravity"),
        ("opencode-native", "opencode"),
        ("native-opencode", "opencode"),
        ("opencode", "opencode"),  # alias folds to opencode-native
        # Non-native harnesses (and the SDK shorthands) have no native pane.
        ("claude-sdk", None),
        ("claude", None),
        ("codex", None),
        ("cursor", None),
        ("agents_sdk", None),
        ("some-unknown-harness", None),
        ("native-prime", None),
        (None, None),
    ],
)
def test_native_terminal_name(harness: str | None, expected: str | None) -> None:
    """``native_terminal_name`` maps native harness ids to their tmux pane name.

    The native-pane idle reaper (#1349) and its turn-path self-heal key panes on
    ``(conv, <short-name>, "main")``; a wrong mapping would reap/relaunch the
    wrong pane or silently skip a harness.
    """
    assert native_terminal_name(harness) == expected


def test_prime_terminal_name_resolves_registered_agent() -> None:
    from omnigent.native.native_coding_agents import native_coding_agent_for_terminal_name

    terminal_name = native_terminal_name("prime-native")
    agent = native_coding_agent_for_terminal_name(terminal_name)

    assert terminal_name == "prime-native"
    assert agent is not None
    assert agent.harness == "prime-native"
    assert agent.agent_name == "prime-native-ui"


@pytest.mark.parametrize("harness", ["future-native", "native-future"])
def test_native_terminal_name_without_agent_metadata(
    monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    monkeypatch.setattr(
        harness_aliases,
        "NATIVE_HARNESSES",
        harness_aliases.NATIVE_HARNESSES | {"future-native", "native-future"},
    )

    assert native_terminal_name(harness) == "future"
