"""Where card artwork is fetched from.

The catalogue's images live on a single CDN, and when that CDN is unreachable
the cards in this installation lose their pictures even though everything else
about them is known locally. TCGDEX_ASSETS_BASE points image fetches at a
mirror instead, which can be a caching proxy on the same LAN.

Two rules shape everything here:

- The mirror is tried first and the canonical CDN second, so turning the
  mirror on cannot make things worse than leaving it off. A mirror that is
  switched off, misconfigured or simply missing an image costs one failed
  request and then behaves exactly as before.
- Only URLs already pointing at the catalogue's own CDN are rewritten. Custom
  card images, uploads and anything a user supplied are left alone: this is a
  mirror of one known host, not a general proxy.
"""

import os
from typing import Optional
from urllib.parse import urlparse, urlunparse

CANONICAL_ASSET_HOST = "assets.tcgdex.net"

# Read once at import, like the catalogue base URLs it sits alongside: this is
# deployment configuration, and an image host that changed halfway through a
# page load would be harder to reason about than a restart.
TCGDEX_ASSETS_BASE = os.environ.get("TCGDEX_ASSETS_BASE", "").rstrip("/")

# Whether the mirror is asked first or only after the CDN has failed.
#
# Both orders try both hosts, so this is a preference rather than a
# restriction. Mirror-primary suits a mirror on the same network: it is
# quicker, it spares the public CDN, and an image already held locally does
# not depend on the internet at all. Mirror-standby suits a mirror you trust
# less than the source, or one you keep purely for the days the CDN is down.
_MIRROR_PRIMARY = "primary"
_MIRROR_STANDBY = "standby"
# "fallback" and "secondary" mean the same thing to most people writing this
# setting, and rejecting them would only produce a mirror silently used in the
# wrong order.
_STANDBY_SPELLINGS = {_MIRROR_STANDBY, "fallback", "secondary"}


def asset_mirror_mode() -> str:
    """Whether the mirror is tried first or second. Defaults to first."""
    configured = os.environ.get("TCGDEX_ASSETS_MODE", "").strip().lower()
    if configured in _STANDBY_SPELLINGS:
        return _MIRROR_STANDBY
    return _MIRROR_PRIMARY


def _mirror_parts():
    """The configured mirror as (scheme, netloc, path prefix), or None."""
    if not TCGDEX_ASSETS_BASE:
        return None
    parsed = urlparse(TCGDEX_ASSETS_BASE)
    if not parsed.scheme or not parsed.netloc:
        return None
    return parsed.scheme, parsed.netloc, parsed.path.rstrip("/")


def is_catalogue_asset(url: object) -> bool:
    """Whether this URL is one of the catalogue CDN's own images."""
    if not url:
        return False
    return urlparse(str(url)).hostname == CANONICAL_ASSET_HOST


def mirror_asset_url(url: object) -> Optional[str]:
    """The mirror's URL for a catalogue image, or None if there is no mirror.

    Returns None for anything that is not a catalogue CDN URL, so a caller can
    hand it any image and get back only a genuine substitution.
    """
    parts = _mirror_parts()
    if parts is None or not is_catalogue_asset(url):
        return None
    scheme, netloc, prefix = parts
    parsed = urlparse(str(url))
    return urlunparse((scheme, netloc, f"{prefix}{parsed.path}", "", parsed.query, ""))


def asset_urls_to_try(url: object) -> list:
    """Where to look for one image, preferred first.

    Both the mirror and the CDN it mirrors appear, in the order
    TCGDEX_ASSETS_MODE asks for. Callers walk the list and stop at the first
    that answers, which is what makes configuring a mirror safe in either
    order: whichever is second is still there behind the first.
    """
    if not url:
        return []
    mirror = mirror_asset_url(url)
    if not mirror:
        return [str(url)]
    if asset_mirror_mode() == _MIRROR_STANDBY:
        return [str(url), mirror]
    return [mirror, str(url)]


def secure_asset_url(url: object) -> str:
    """The mirror's URL only when the mirror is itself over HTTPS.

    Used by the paths that download reference artwork to compare against a
    user's photo. Those require HTTPS on purpose, because the bytes are fed to
    an image decoder and on to the vision model, and a mirror on a plain HTTP
    LAN address must not quietly relax that. Such a mirror is simply not used
    for this, and the canonical CDN is.
    """
    if asset_mirror_mode() == _MIRROR_STANDBY:
        # One URL is chosen here with no second attempt, so a mirror the
        # operator has asked to keep in reserve is not the one to pick.
        return str(url) if url else ""
    mirror = mirror_asset_url(url)
    if mirror and urlparse(mirror).scheme == "https":
        return mirror
    return str(url) if url else ""


def trusted_asset_hosts() -> set:
    """Hosts the reference-image download may fetch from.

    The catalogue CDN always, plus an HTTPS mirror when one is configured. An
    HTTP mirror is deliberately absent: see secure_asset_url.
    """
    hosts = {CANONICAL_ASSET_HOST}
    parts = _mirror_parts()
    if parts is not None and asset_mirror_mode() != _MIRROR_STANDBY:
        scheme, _netloc, _prefix = parts
        if scheme == "https":
            hosts.add(urlparse(TCGDEX_ASSETS_BASE).hostname)
    return hosts
