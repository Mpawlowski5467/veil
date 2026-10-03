"""Harmless code references that should remain readable during a beta exercise."""


def connect(settings):
    """Return configuration references; no connection is made."""
    password: str = settings.database_password
    api_key = settings.api_key
    return password, api_key
