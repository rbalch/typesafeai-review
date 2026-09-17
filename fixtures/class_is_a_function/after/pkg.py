def helper() -> int:
    return 1


class Greeter:
    def __init__(self, name: str) -> None:
        self.name = name

    def greet(self) -> str:
        return f'Hello, {self.name}!'
