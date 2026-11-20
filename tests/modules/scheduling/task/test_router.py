from tests.factories import accept, events, video_message


async def test_control_of_unknown_job_returns_404(client):
    assert (await client.post("/api/jobs/missing/retry")).status_code == 404
    assert (await client.post("/api/jobs/missing/cancel")).status_code == 404


async def test_cancelling_a_batch_returns_the_cancelled_jobs(client, database):
    await accept(database, video_message("j-1"))
    await accept(database, video_message("j-2"))

    response = await client.post("/api/batches/b-1/cancel")

    assert response.status_code == 200
    assert [(job["job_id"], job["status"]) for job in response.json()] == [
        ("j-1", "cancelled"),
        ("j-2", "cancelled"),
    ]
    # 已取消的任务不会再次取消。
    assert (await client.post("/api/batches/b-1/cancel")).json() == []


async def test_jobs_arriving_after_the_batch_is_cancelled_are_not_accepted(client, database):
    assert (await client.post("/api/batches/b-1/cancel")).json() == []  # 可以先于任务取消

    await accept(database, video_message("j-1"))
    await accept(database, video_message("j-2", batch="b-2"))

    assert [event["job_id"] for event in await events(database)] == ["j-2"]


async def test_retrying_a_job_without_failures_changes_nothing(client, database):
    await accept(database, video_message())

    response = await client.post("/api/jobs/j-1/retry")

    assert (response.json()["job_id"], response.json()["status"]) == ("j-1", "accepted")
