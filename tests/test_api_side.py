from fastapi.testclient import TestClient

from api_side.main import app, latest_results, result_history


def test_receive_and_list_latest() -> None:
    latest_results.clear()
    result_history.clear()
    client = TestClient(app)

    response = client.post(
        "/checks",
        json={
            "client_id": "client-1",
            "server_id": "vps-1",
            "server_name": "VPS 1",
            "ok": True,
            "comment": "tunnel is reachable",
            "duration_ms": 123,
        },
    )
    assert response.status_code == 200

    latest_response = client.get("/checks/latest")
    assert latest_response.status_code == 200
    assert latest_response.json()["results"][0]["client_id"] == "client-1"
    assert latest_response.json()["results"][0]["server_id"] == "vps-1"
