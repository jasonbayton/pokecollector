"""Installation-wide scanner keys, as a fallback behind each user's own.

Fork-local: upstream has no cross-user key fallback at all. Kept in its own
module so scan_providers.py stays identical to the branch offered upstream and
future merges do not conflict on it.

Resolution order is the user's own key, then the environment. Setting the
environment variable is itself the operator's decision to provide a key for
everyone; leaving it unset means each account must supply its own. There is no
separate opt-in flag, because the presence of the key is the opt-in.
"""

import os

# The environment variable holding each provider's installation-wide key.
SHARED_ENV_KEYS = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}


def shared_env_key(provider_name: str) -> str:
    """The installation-wide key for this provider, or an empty string."""
    env_name = SHARED_ENV_KEYS.get(provider_name)
    if not env_name:
        return ""
    return os.environ.get(env_name, "").strip()
