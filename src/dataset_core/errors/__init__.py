class DatasetCoreError(Exception):
    """Stable structured error for adapters to convert into API/UI errors."""

    def __init__(self, code: str, *, params: dict | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.params = params or {}
