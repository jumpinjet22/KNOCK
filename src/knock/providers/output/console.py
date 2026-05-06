class ConsoleOutputProvider:
    name = "console-output"

    def send(self, text: str) -> None:
        print(text)
