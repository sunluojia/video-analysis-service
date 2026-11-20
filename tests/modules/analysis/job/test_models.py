from datetime import UTC, datetime

from video_analysis.modules.analysis.job.models import Job, JobKind, JobStatus, MediaInfo

NOW = datetime(2026, 11, 17, tzinfo=UTC)


def make_job(status=JobStatus.PROCESSING) -> Job:
    job = Job(JobKind.VIDEO, "j-1", 1, "b-1", "c-1", {})
    job.status = status
    return job


def test_recognition_waits_for_frames_and_every_audio_segment():
    job = make_job()
    job.fetched(MediaInfo(duration=30, has_audio=True))
    job.frame_count = 8
    assert not job.ready_for_recognition(0)
    job.segment_count = 2
    assert not job.ready_for_recognition(1)
    assert job.ready_for_recognition(2)


def test_video_without_audio_needs_no_listening():
    job = make_job()
    job.fetched(MediaInfo(duration=30, has_audio=False))
    job.frame_count = 8
    assert job.ready_for_recognition(0)


def test_failure_outranks_review():
    job = make_job(JobStatus.FAILED)
    job.hold_for_review("timeout", NOW)
    assert job.status == JobStatus.FAILED


def test_reuse_takes_the_earlier_result_without_artifacts():
    earlier = make_job(JobStatus.COMPLETED)
    earlier.duration, earlier.analysis_key, earlier.result = 30, "key", {"recognition": {}}
    job = Job(JobKind.VIDEO, "j-2", 1, "b-1", "c-1", {})

    job.reuse(earlier, NOW)

    assert (job.status, job.result, job.reused_from) == (JobStatus.COMPLETED, earlier.result, "j-1")
    assert job.artifacts_until is None


def test_new_run_discards_the_previous_progress():
    job = make_job(JobStatus.COMPLETED)
    job.fetched(MediaInfo(duration=30, has_audio=False))
    job.result, job.analysis_key, job.artifacts_until = {"recognition": {}}, "key", NOW

    job.restart(2, {"description": "新"}, NOW)

    assert (job.run, job.status, job.inputs) == (2, JobStatus.ACCEPTED, {"description": "新"})
    assert (job.duration, job.segment_count, job.result, job.analysis_key) == (None,) * 4
    assert job.artifacts_until is None


def test_cancelled_job_releases_its_files_now():
    job = make_job()
    job.cancel(NOW)
    assert (job.status, job.artifacts_until) == (JobStatus.CANCELLED, NOW)
