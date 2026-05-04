"""Identify the active Claude account.

Two complementary signals:

1. **``credentials.json``** in the active ``CLAUDE_CONFIG_DIR`` —
   exposes ``subscriptionType`` and ``rateLimitTier``. These differ
   between Team / Pro / Max plans, giving a stable per-account
   fingerprint.

2. **TUI welcome panel** — when ``claude`` starts, the welcome panel
   shows a line like ``Opus 4.7 (1M context) · Claude Team · Human
   Predictions``. We extract the dot-separated tail as the
   organization label.

Combined into :class:`IdentitySnapshot`, the user can verify they are
operating against the intended account before trusting any usage
numbers — see :func:`from_capture` for the live path.

The OAuth access token is opaque (not a JWT), so identity claims
cannot be decoded from it directly.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from claude_task_runner.usage import render as render_mod

CREDENTIALS_FILENAME = ".credentials.json"

# Welcome panel sentinel: lines look roughly like
#   "Opus 4.7 (1M context) · Claude Team · Human Predictions"
# We extract the chain of bullet-separated identity tokens.
_WELCOME_DOT_LINE_RE = re.compile(
    r"(?:Opus|Sonnet|Haiku|Claude)[^\n·]*·\s*([^·\n]+(?:\s*·\s*[^·\n]+)*)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class IdentitySnapshot:
    """A single read of the active Claude account identity.

    Empty fields signal "not available" (e.g. no credentials file
    present, or the welcome panel format has changed).
    """

    config_dir: str
    """The CLAUDE_CONFIG_DIR in effect when this snapshot was taken,
    or the empty string for the default ``~/.claude``."""

    subscription_type: str = ""
    """``team``, ``pro``, ``max5``, ``max20``, etc. From credentials.json."""

    rate_limit_tier: str = ""
    """E.g. ``default_claude_max_5x``. From credentials.json."""

    scopes: tuple[str, ...] = ()
    """OAuth scopes granted to the current token."""

    welcome_label: str = ""
    """Bullet-joined identity tail from the TUI welcome panel,
    e.g. ``"Claude Team · Human Predictions"``. Empty if not captured."""

    def is_personal(self) -> bool:
        """Heuristic: subscription_type starts with ``pro`` / ``max`` and
        is not the Team plan. Operators can override by setting an
        explicit expected_subscription_type in their queue config."""
        st = self.subscription_type.lower()
        return st.startswith(("pro", "max")) and st != "team"

    def is_team(self) -> bool:
        """Heuristic: Team / Enterprise plan."""
        return self.subscription_type.lower() in {"team", "enterprise"}


def credentials_path(claude_config_dir: str = "") -> Path:
    """Resolve the active ``credentials.json`` path.

    Empty ``claude_config_dir`` means use the default ``~/.claude``.
    """
    base = Path(claude_config_dir).expanduser() if claude_config_dir else Path.home() / ".claude"
    return base / CREDENTIALS_FILENAME


def read_credentials(claude_config_dir: str = "") -> dict[str, str | tuple[str, ...]]:
    """Read identity-relevant fields from ``credentials.json``.

    Returns a dict with at most ``subscription_type``, ``rate_limit_tier``,
    and ``scopes``. Missing fields are absent rather than empty so the
    caller can detect a totally-missing credentials file.
    """
    path = credentials_path(claude_config_dir)
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return {}
    out: dict[str, str | tuple[str, ...]] = {}
    if isinstance(sub := oauth.get("subscriptionType"), str):
        out["subscription_type"] = sub
    if isinstance(tier := oauth.get("rateLimitTier"), str):
        out["rate_limit_tier"] = tier
    scopes = oauth.get("scopes")
    if isinstance(scopes, list) and all(isinstance(s, str) for s in scopes):
        out["scopes"] = tuple(scopes)
    return out


_LABEL_CONTINUATION_RE = re.compile(r"^[A-Z][\w\s.&'/-]{1,80}$")


def extract_welcome_label(rendered_text: str) -> str:
    """Extract the org-identity line from a rendered welcome panel.

    The TUI uses a two-column layout where ``│`` separates the welcome
    panel (left) from the "Tips" sidebar (right). We:

    1. Split each line on column separators so the sidebar's text
       cannot bleed into the label.
    2. Find the column containing the ``... · Claude Team · ...``
       pattern.
    3. If the org name wraps to the *next* line within the same column
       (e.g., ``"... · Human"`` followed on the next row by
       ``"Predictions"``), append the continuation. We accept a
       continuation only if it looks like a word/short phrase (capital
       letter start, no exotic punctuation) so unrelated panel content
       cannot stitch on.

    Returns the empty string if no recognizable label line appears.
    """
    rows = rendered_text.splitlines()
    columns_per_row = [re.split(r"[│┃|]+", row) for row in rows]

    for row_idx, columns in enumerate(columns_per_row):
        for col_idx, column in enumerate(columns):
            stripped = column.strip()
            if not stripped:
                continue
            match = _WELCOME_DOT_LINE_RE.search(stripped)
            if match is None:
                continue

            label = match.group(1).strip()

            # Look at the SAME column position on the next non-empty row
            # for a continuation of the wrapped org name.
            for follow_idx in range(row_idx + 1, min(row_idx + 4, len(columns_per_row))):
                follow_columns = columns_per_row[follow_idx]
                if col_idx >= len(follow_columns):
                    break
                follow = follow_columns[col_idx].strip()
                if not follow:
                    continue
                if _LABEL_CONTINUATION_RE.match(follow):
                    label = f"{label} {follow}"
                # First non-empty follower is decisive; stop either way.
                break

            return re.sub(r"\s+", " ", label)
    return ""


def from_capture(
    raw_capture: bytes,
    claude_config_dir: str = "",
) -> IdentitySnapshot:
    """Build an :class:`IdentitySnapshot` from a captured ``/usage``
    invocation's raw bytes.

    The same ``.cap`` we already collect for usage parsing carries the
    welcome panel — no extra subprocess needed.
    """
    creds = read_credentials(claude_config_dir)
    rendered = render_mod.render(raw_capture) if raw_capture else ""
    welcome = extract_welcome_label(rendered)
    return IdentitySnapshot(
        config_dir=claude_config_dir,
        subscription_type=str(creds.get("subscription_type", "")),
        rate_limit_tier=str(creds.get("rate_limit_tier", "")),
        scopes=tuple(creds.get("scopes", ())) if isinstance(creds.get("scopes"), tuple) else (),
        welcome_label=welcome,
    )


def from_credentials_only(claude_config_dir: str = "") -> IdentitySnapshot:
    """Build a snapshot from credentials.json alone — no TUI capture.

    Useful for fast `whoami` invocations that don't need to spawn
    ``claude``. The welcome label is left empty.
    """
    creds = read_credentials(claude_config_dir)
    return IdentitySnapshot(
        config_dir=claude_config_dir,
        subscription_type=str(creds.get("subscription_type", "")),
        rate_limit_tier=str(creds.get("rate_limit_tier", "")),
        scopes=tuple(creds.get("scopes", ())) if isinstance(creds.get("scopes"), tuple) else (),
        welcome_label="",
    )
