import time

from test_call_flow import _answered_call, _handshake


def test_websocket_abrupt_disconnect(monkeypatch, quiet_providers):
    client, store, params = _answered_call(monkeypatch)
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        time.sleep(0.2)
        ws.close()
    call = store.get("calls", params["callId"])
    assert call is not None
    assert call["status"] == "ended" or call.get("endedReason")
