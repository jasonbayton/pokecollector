"""Text search helpers for user-facing filters."""

from __future__ import annotations

import sqlite3
import unicodedata

from sqlalchemy import event, func, literal, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

# Keep a small per-engine cache so PostgreSQL extension probing is cheap and so
# installs where CREATE EXTENSION is not permitted gracefully use the portable
# fallback instead of failing every search request.
_UNACCENT_AVAILABLE_BY_BIND: dict[int, bool] = {}

# Same idea for the SQLite helper function below: probe once per engine so a
# connection that somehow never had the function registered falls back instead
# of failing every search request.
_SQLITE_UNACCENT_AVAILABLE_BY_BIND: dict[int, bool] = {}

# Name of the application-defined SQLite function registered on every SQLite
# connection. Prefixed so it cannot collide with a built-in or with another
# library's helper.
SQLITE_UNACCENT_FUNCTION = "pokecollector_unaccent"

# Portable fallback used for SQLite tests and PostgreSQL installs where the
# unaccent extension cannot be enabled. Keep this deliberately small to avoid
# creating overly deep SQL expression trees on SQLite; PostgreSQL unaccent is
# still the full production path when available.
_LATIN_REPLACEMENTS = {
    "a": "áàâäãåÁÀÂÄÃÅ",
    "c": "çÇ",
    "e": "éèêëÉÈÊË",
    "i": "íìîïÍÌÎÏ",
    "n": "ñÑ",
    "o": "óòôöõøÓÒÔÖÕØ",
    "u": "úùûüÚÙÛÜ",
    "y": "ýÿÝŸ",
}

# Character-for-character arguments to PostgreSQL's built-in translate(), which
# expresses the whole table above in a single call. Every replacement target is
# a plain ASCII letter that never appears as a source, so a one-pass translate
# and the sequential replace() chain below produce the same text.
_TRANSLATE_SOURCE = "".join(_LATIN_REPLACEMENTS.values())
_TRANSLATE_TARGET = "".join(
    base * len(characters) for base, characters in _LATIN_REPLACEMENTS.items()
)


def strip_diacritics(value: str | None) -> str:
    """Return a case-folded, accent-insensitive representation of text."""
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKD", str(value))
    stripped = "".join(char for char in normalized if not unicodedata.combining(char))
    return stripped.casefold()


@event.listens_for(Engine, "connect")
def _register_sqlite_unaccent(dbapi_connection, connection_record) -> None:
    """Expose strip_diacritics to SQL on every SQLite connection.

    SQLite has no unaccent extension and no translate(), so the only portable
    alternative is one nested replace() per accented character. That expression
    is deeper than the parser stack of older SQLite builds, which reject it with
    "parser stack overflow", so the whole search feature was untestable on any
    host shipping an older library. An application-defined function collapses it
    to a single call, and reuses the same normalisation as the search term.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    try:
        dbapi_connection.create_function(
            SQLITE_UNACCENT_FUNCTION, 1, strip_diacritics, deterministic=True
        )
    except Exception:
        # deterministic= needs a recent enough SQLite; it is only an optimiser
        # hint, so registering without it is still correct.
        dbapi_connection.create_function(SQLITE_UNACCENT_FUNCTION, 1, strip_diacritics)


def _sqlite_unaccent_available(db: Session) -> bool:
    bind = db.get_bind()
    if bind.dialect.name != "sqlite":
        return False

    cache_key = id(bind)
    if cache_key in _SQLITE_UNACCENT_AVAILABLE_BY_BIND:
        return _SQLITE_UNACCENT_AVAILABLE_BY_BIND[cache_key]

    try:
        db.execute(text(f"SELECT {SQLITE_UNACCENT_FUNCTION}('Pokegear')")).scalar()
        available = True
    except Exception:
        db.rollback()
        available = False

    _SQLITE_UNACCENT_AVAILABLE_BY_BIND[cache_key] = available
    return available


def _portable_unaccent_expr(db: Session, column):
    """Build an accent-insensitive column expression without the unaccent extension.

    Expression depth matters here. The generic form nests one replace() per
    accented character, which is deep enough to overflow the parser stack of
    older SQLite builds. Both supported backends therefore get a constant-depth
    expression instead: a single translate() on PostgreSQL, a single
    application-defined function on SQLite. The nested chain is kept only as a
    last resort for any other dialect.
    """
    if _sqlite_unaccent_available(db):
        return getattr(func, SQLITE_UNACCENT_FUNCTION)(column)

    if db.get_bind().dialect.name == "postgresql":
        return func.translate(
            func.lower(column), _TRANSLATE_SOURCE, _TRANSLATE_TARGET
        )

    expr = func.lower(column)
    for replacement, characters in _LATIN_REPLACEMENTS.items():
        for character in characters:
            expr = func.replace(expr, character, replacement)
    return expr


def _postgres_unaccent_available(db: Session) -> bool:
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return False

    cache_key = id(bind)
    if cache_key in _UNACCENT_AVAILABLE_BY_BIND:
        return _UNACCENT_AVAILABLE_BY_BIND[cache_key]

    try:
        db.execute(text("SELECT unaccent('Pokégear')")).scalar()
        available = True
    except Exception:
        db.rollback()
        available = False

    _UNACCENT_AVAILABLE_BY_BIND[cache_key] = available
    return available


def accent_insensitive_contains(db: Session, column, value: str | None):
    """Build a SQL predicate for accent-insensitive substring search."""
    if not value:
        return None

    # A search box is not a pattern language. Without this, typing % matches
    # every row and _ matches any character, which is never what somebody
    # looking for a card meant.
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    if _postgres_unaccent_available(db):
        pattern = f"%{escaped}%"
        return func.unaccent(func.lower(column)).like(
            func.unaccent(func.lower(literal(pattern))), escape="\\"
        )

    normalized = strip_diacritics(escaped)
    if not normalized:
        return None
    return _portable_unaccent_expr(db, column).like(f"%{normalized}%", escape="\\")
