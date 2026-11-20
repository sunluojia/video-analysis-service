from tests.factories import accept, load_job, video_message


async def test_unknown_job_returns_404(client):
    assert (await client.get("/api/jobs/missing")).status_code == 404


async def test_job_detail_shows_its_stages(client, database):
    await accept(database, video_message())

    response = await client.get("/api/jobs/j-1")

    assert response.status_code == 200
    detail = response.json()
    assert (detail["job"]["job_id"], detail["job"]["status"]) == ("j-1", "accepted")
    assert [(task["kind"], task["status"]) for task in detail["tasks"]] == [("fetch", "ready")]
    assert detail["ai_calls"] == []


async def test_frames_are_served_within_the_retention_period(client, database, tmp_path):
    await accept(database, video_message())
    job = await load_job(database)
    frames = tmp_path / "work" / str(job.id) / "frames"
    frames.mkdir(parents=True)
    (frames / "001.jpg").write_bytes(b"jpg")

    found = await client.get("/api/jobs/j-1/frames/1")
    missing = await client.get("/api/jobs/j-1/frames/2")

    assert (found.status_code, found.content) == (200, b"jpg")
    assert found.headers["content-type"] == "image/jpeg"
    assert missing.status_code == 404
