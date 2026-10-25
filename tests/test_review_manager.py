"""Retired submissions never create outbound HTTP clients."""
import httpx
import pytest
from whisperstream.review_manager import ReviewManager

@pytest.mark.asyncio
@pytest.mark.parametrize("method",["submit_review","submit_feedback"])
async def test_retired_submission_is_unavailable_without_network(monkeypatch,method):
    monkeypatch.setattr(httpx,"AsyncClient",lambda *a,**kw:pytest.fail("unexpected network"))
    result=await getattr(ReviewManager(),method)({"rating":5,"feedback":"synthetic","customer_email":"synthetic@example.test"})
    assert result["success"] is False
    assert "About" in result["error"]
