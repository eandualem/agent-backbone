"""Stable, bounded Sheets refusals shared by storage, workers and HTTP."""


class SheetError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422, **context):
        super().__init__(message)
        self.status = status
        self.detail = {"code": code, "message": message, **context}
