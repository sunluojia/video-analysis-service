import socket
from uuid import uuid4

from tests.factories import SERVICE_TOKEN
from video_analysis.core.config.settings import WorkerSettings


def test_worker_id_defaults_to_the_hostname(monkeypatch):
    monkeypatch.delenv("VIDEO_ANALYSIS_WORKER_ID", raising=False)
    monkeypatch.delenv("VA_WORKER_ID", raising=False)

    settings = WorkerSettings(service_token=SERVICE_TOKEN, _env_file=None)

    assert settings.worker_id == socket.gethostname()


def test_worker_defaults_to_six_frames(monkeypatch):
    monkeypatch.delenv("VA_FRAME_COUNT", raising=False)

    settings = WorkerSettings(service_token=SERVICE_TOKEN, _env_file=None)

    assert settings.frame_count == 6


def test_worker_identity_keeps_the_configured_id_and_generates_a_session_per_start(monkeypatch):
    supplied_session = uuid4()
    monkeypatch.setenv("VIDEO_ANALYSIS_WORKER_ID", "analysis-worker-a")
    monkeypatch.setenv("VA_WORKER_SESSION_ID", str(supplied_session))

    first = WorkerSettings(service_token=SERVICE_TOKEN, _env_file=None)
    second = WorkerSettings(service_token=SERVICE_TOKEN, _env_file=None)

    assert first.worker_id == second.worker_id == "analysis-worker-a"
    assert first.worker_session_id.version == second.worker_session_id.version == 4
    assert len({first.worker_session_id, second.worker_session_id, supplied_session}) == 3
