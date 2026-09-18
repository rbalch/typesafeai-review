import os


def get_secret() -> str | None:
    return os.environ.get('API_KEY')
