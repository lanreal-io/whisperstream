"""Compatibility responses for retired feedback endpoints."""


class ReviewManager:
    def __init__(self, logger=None) -> None:
        pass

    async def submit_review(self, data: dict) -> dict:
        return {"success": False, "error": "In-app feedback submission is unavailable. Use About to report a bug."}

    async def submit_feedback(self, data: dict) -> dict:
        return await self.submit_review(data)
