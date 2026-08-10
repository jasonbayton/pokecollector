"""Whether one installation-wide scanner key may serve every account.

Kept as its own module, deliberately. This is fork-local policy that upstream
does not have, and scan_providers.py is shared with the branch offered upstream,
so putting it there would make every future merge conflict on that file.
"""

import os


def shared_scanner_key_allowed() -> bool:
    """Whether an installation-wide key may be used for any account.

    Off by default: the key belongs to whoever pays for the deployment, so
    sharing it with every authenticated account is a billing and isolation
    decision the operator has to make deliberately. With it off, behaviour
    matches the per-user model the project documents.
    """
    return os.environ.get("ALLOW_SHARED_SCANNER_KEY", "").strip().lower() in {
        "true", "1", "yes", "on",
    }


# The environment variable holding each provider's installation-wide key.
SHARED_ENV_KEYS = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}


def shared_env_key(provider_name: str) -> str:
    """The installation-wide key for this provider, if sharing is enabled.

    Returns an empty string when sharing is off, which is the default, so the
    per-user model is unchanged unless the operator opts in.
    """
    if not shared_scanner_key_allowed():
        return ""
    env_name = SHARED_ENV_KEYS.get(provider_name)
    if not env_name:
        return ""
    return os.environ.get(env_name, "").strip()
