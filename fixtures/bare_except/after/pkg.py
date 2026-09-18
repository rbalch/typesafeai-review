def load_config(path: str) -> dict:
    try:
        with open(path) as f:
            return {}
    except:
        return {}
