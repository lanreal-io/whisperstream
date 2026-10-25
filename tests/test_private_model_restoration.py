"""Remote model manifests stay retired."""
import pytest
from whisperstream import model_downloader as d

def test_retired_manifest_cannot_make_request():
    class Client:
        def get(self, *args):
            pytest.fail("unexpected network")
    with pytest.raises(RuntimeError, match="Remote model manifests are unavailable"):
        d.fetch_manifest(Client())
