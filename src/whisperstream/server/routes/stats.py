"""Usage statistics HTTP projection."""

from fastapi import FastAPI

from ..stats import UsageStats


def register_stats_routes(api: FastAPI, stats: UsageStats) -> None:
    @api.get("/api/stats")
    async def get_stats():
        return stats.to_dict()
