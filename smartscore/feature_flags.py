import os


def _get_bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "on"}


FLAGS = {
    "send_emails": _get_bool_env("FEATURE_SEND_EMAILS", default=False),
}


def is_feature_enabled(name: str) -> bool:
    return FLAGS.get(name, False)
