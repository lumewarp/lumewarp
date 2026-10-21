"""Cursor pagination on entity lists and invalid cursors."""

from __future__ import annotations

from tests.api.helpers import BOOTSTRAP_AUTH, suffix


def test_org_and_team_pagination(client) -> None:
    org_ids = []
    for _ in range(3):
        resp = client.post(
            "/api/v1/orgs",
            json={"name": f"page-{suffix()}"},
            headers=BOOTSTRAP_AUTH,
        )
        assert resp.status_code == 201
        org_ids.append(resp.json()["id"])

    seen: set[str] = set()
    cursor = None
    for _page in range(100):
        url = "/api/v1/orgs?limit=2"
        if cursor is not None:
            url += f"&cursor={cursor}"
        resp = client.get(url, headers=BOOTSTRAP_AUTH)
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) <= 2
        ids = {item["id"] for item in items}
        assert not seen & ids
        seen |= ids
        cursor = resp.json()["next_cursor"]
        if cursor is None:
            break
    else:
        raise AssertionError("pagination did not terminate")
    assert set(org_ids) <= seen
    assert client.get("/api/v1/orgs?cursor=nope", headers=BOOTSTRAP_AUTH).status_code == 422

    org = org_ids[0]
    team_ids = []
    for _ in range(3):
        resp = client.post(
            f"/api/v1/orgs/{org}/teams",
            json={"name": f"page-team-{suffix()}"},
            headers=BOOTSTRAP_AUTH,
        )
        assert resp.status_code == 201
        team_ids.append(resp.json()["id"])
    page = client.get(f"/api/v1/orgs/{org}/teams?limit=2", headers=BOOTSTRAP_AUTH)
    assert page.status_code == 200
    assert len(page.json()["items"]) == 2
    assert page.json()["next_cursor"]
    rest = client.get(
        f"/api/v1/orgs/{org}/teams?limit=2&cursor={page.json()['next_cursor']}",
        headers=BOOTSTRAP_AUTH,
    )
    got = {item["id"] for item in page.json()["items"] + rest.json()["items"]}
    assert set(team_ids) <= got
