from sqlalchemy import select

from video_analysis.modules.analysis.batch.tables import batches


async def test_batch_can_be_paused_before_its_jobs_arrive(client, database):
    assert (await client.post("/api/batches/b-9/pause")).status_code == 204

    async with database.engine.connect() as connection:
        paused = await connection.scalar(select(batches.c.paused).where(batches.c.id == "b-9"))
    assert paused is True

    assert (await client.post("/api/batches/b-9/resume")).status_code == 204
