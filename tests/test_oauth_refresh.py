import json
from unittest.mock import patch
from app.auth import oauth

DUMMY_REFRESH = "dummy-refresh-token"

class DummyResp:
    def __init__(self, status_code=200, json_data=None, text_data=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text_data or (json.dumps(json_data) if isinstance(json_data, dict) else "")
    def json(self):
        if self._json is None:
            raise ValueError("No JSON")
        return self._json

@patch("app.auth.oauth.requests.post")
def test_refresh_success(mock_post):
    mock_post.return_value = DummyResp(200, {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600})
    token = oauth.refresh_token(DUMMY_REFRESH)
    assert token["access_token"] == "new-access"

@patch("app.auth.oauth.requests.post")
def test_refresh_http_error(mock_post):
    mock_post.return_value = DummyResp(400, {"error": "invalid_grant"})
    try:
        oauth.refresh_token(DUMMY_REFRESH)
        assert False, "Expected RuntimeError"
    except RuntimeError as e:
        assert "HTTP 400" in str(e)

@patch("app.auth.oauth.requests.post")
def test_refresh_non_json_body(mock_post):
    mock_post.return_value = DummyResp(200, None, text_data="<html>Error</html>")
    try:
        oauth.refresh_token(DUMMY_REFRESH)
        assert False, "Expected RuntimeError"
    except RuntimeError as e:
        assert "non-JSON" in str(e)
