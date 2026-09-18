import os


def get_secret() -> str:
    return os.environ.get('API_KEY', '')
