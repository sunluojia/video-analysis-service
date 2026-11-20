async def test_ai_configuration(client):
    provider = await client.post(
        "/api/ai/providers",
        json={"name": "openai", "provider": "openai", "credential_ref": "OPENAI_API_KEY"},
    )
    model = await client.post(
        "/api/ai/models", json={"provider_id": provider.json()["id"], "name": "gpt-test"}
    )
    model_id = model.json()["id"]

    route = await client.put("/api/ai/routes/recognize", json={"model_id": model_id, "share": 6})

    assert route.json() == {"kind": "recognize", "model_id": model_id, "share": 6}
    invalid_kind = await client.put("/api/ai/routes/probe", json={"model_id": model_id})
    assert invalid_kind.status_code == 422
    assert (await client.delete(f"/api/ai/models/{model_id}")).status_code == 409
