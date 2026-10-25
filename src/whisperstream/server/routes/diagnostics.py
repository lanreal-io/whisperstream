"""Diagnostic ingest and download endpoints."""

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..diagnostics import DiagnosticLogs, _normalize_frontend_log_source


class LogExportBody(BaseModel):
    frontend_logs: list[str]
    source: str | None = None  # webview identifier (main, popup-review, etc.)


def register_diagnostic_routes(api: FastAPI, logs: DiagnosticLogs) -> None:
    @api.post("/api/logs/frontend")
    async def sync_frontend_logs(body: LogExportBody):
        source = _normalize_frontend_log_source(body.source or "main")
        source, count = logs.store(source, body.frontend_logs)
        return {
            "status": "ok",
            "source": source,
            "count": count,
        }

    @api.post("/api/logs/export")
    async def export_logs_post(body: LogExportBody):
        merged = logs.merge_frontend_logs(
            override=body.frontend_logs,
            override_source=_normalize_frontend_log_source(body.source or "main"),
        )
        buf = logs.build_log_zip(merged)
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="whisperstream-logs.zip"'
            },
        )

    @api.get("/api/logs/export")
    async def export_logs_get():
        merged = logs.merge_frontend_logs()
        buf = logs.build_log_zip(merged)
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="whisperstream-logs.zip"'
            },
        )
