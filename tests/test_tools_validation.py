import pytest

from openring.tools import ToolError, check_tool


def test_apiRequest_validation():
    valid = {
        "type": "apiRequest",
        "name": "send_alert",
        "method": "POST",
        "url": "https://example.com/webhook",
        "body": {"type": "object", "properties": {"message": {"type": "string"}}},
    }
    check_tool(valid)

    invalid_method = {**valid, "method": "INVALID"}
    with pytest.raises(ToolError):
        check_tool(invalid_method)

    invalid_name = {**valid, "name": "bad name!"}
    with pytest.raises(ToolError):
        check_tool(invalid_name)

    with_cred = {**valid, "credentialId": "secret-cred"}
    with pytest.raises(ToolError):
        check_tool(with_cred)
