async def active_versions(client) -> list[tuple[int, bool]]:
    versions = (await client.get("/api/ai/prompts/recognize")).json()["versions"]
    return [(version["version"], version["active"]) for version in versions]


async def test_prompt_versions_can_be_switched_and_rolled_back(client):
    history = (await client.get("/api/ai/prompts/recognize")).json()
    assert history["versions"] == []
    assert "tags" in history["builtin"]

    first = await client.post("/api/ai/prompts/recognize", json={"content": "A", "note": "初版"})
    await client.post("/api/ai/prompts/recognize", json={"content": "B", "note": "细化标签"})
    assert await active_versions(client) == [(2, True), (1, False)]

    await client.put("/api/ai/prompts/recognize/active", json={"prompt_id": first.json()["id"]})
    assert await active_versions(client) == [(2, False), (1, True)]

    await client.put("/api/ai/prompts/recognize/active", json={"prompt_id": None})
    assert await active_versions(client) == [(2, False), (1, False)]
