from tests.factories import accept, events, load_job, video_message
from video_analysis.modules.analysis.job.models import JobStatus
from video_analysis.modules.scheduling.task.models import TaskKind
from video_analysis.modules.scheduling.task.repository import TaskRepository


async def stage_kinds(database) -> list[TaskKind]:
    job = await load_job(database)
    async with database.transaction() as session:
        return [task.kind for task in await TaskRepository(session).of_job(job.id)]


async def test_accepted_job_starts_with_fetching_the_video(database):
    await accept(database, video_message())

    [event] = await events(database)
    assert (event["type"], event["job_id"], event["status"]) == ("job", "j-1", "accepted")
    assert await stage_kinds(database) == [TaskKind.FETCH]


async def test_duplicate_delivery_is_ignored(database):
    await accept(database, video_message())
    await accept(database, video_message())

    assert len(await events(database)) == 1
    assert await stage_kinds(database) == [TaskKind.FETCH]


async def test_higher_run_restarts_and_older_run_is_ignored(database):
    await accept(database, video_message())
    await accept(database, video_message(description="新描述", run=3))
    await accept(database, video_message(run=2))

    job = await load_job(database)
    assert (job.run, job.status, job.video.description) == (3, JobStatus.ACCEPTED, "新描述")
    assert [event["run"] for event in await events(database)] == [1, 3]
    assert await stage_kinds(database) == [TaskKind.FETCH]
