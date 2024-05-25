class DownloadError(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)


class MessageTooLongError(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)


class EmailFormatError(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)


class PdfError(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)
