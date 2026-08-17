from __future__ import annotations


class ParseError(Exception):
    """Exception for when and error occurs during parsing and is unable to continue/complete"""

    def __init__(self, *args, text: str) -> None:
        """
        :param text: The text that was being parsed when the error occurred
        """
        super().__init__(*args)
        self.text = text

    def __str__(self) -> str:
        base = super().__str__()
        addon = f"While parsing: \"{self.text}\""
        return base + " " + addon
