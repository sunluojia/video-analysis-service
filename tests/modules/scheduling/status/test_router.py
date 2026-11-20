from tests.factories import accept, configure_ai, video_message


async def test_status_shows_waiting_work_and_model_capacity(client, database):
    await configure_ai(database, capacity=3)
    await accept(database, video_message())

    status = (await client.get("/api/status")).json()

    assert status["jobs"]["video"] == {"accepted": 1}
    assert status["ready"] == {"fetch": 1}
    assert status["running"] == {}
    [model] = status["models"]
    assert (model["capacity"], model["running"], model["recent_requests"]) == (3, 0, 0)
    assert status["free_disk_bytes"] > 0
