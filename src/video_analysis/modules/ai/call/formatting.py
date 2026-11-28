import json
from collections.abc import Sequence

from video_analysis.modules.analysis.job.results import PeopleInFrame, Recognition
from video_analysis.modules.scheduling.execution.ports import VideoMaterial


def recognition_prompt(material: VideoMaterial, frame_count: int) -> str:
    facts = {
        "发布描述": material.description,
        "发布话题": material.topics,
        "声音类型": material.listening.sounds,
        "讲话文本": material.listening.transcript,
    }
    return f"视频画面共 {frame_count} 张，按时间顺序附后，序号从 0 开始。\n{_json(facts)}"


def filter_frame_evidence(recognition: Recognition, frame_count: int) -> None:
    people = recognition.people
    if (
        people
        and people.views
        and not (people.frame_index is not None and 0 <= people.frame_index < frame_count)
    ):
        recognition.people = PeopleInFrame(views=[])
    recognition.appearances = [
        item
        for item in recognition.appearances
        if 0 <= item.frame_index < frame_count
        and (item.age_group or item.skin_tone)
        and item.evidence.strip()
    ]


def summary_prompt(videos: Sequence[Recognition]) -> str:
    results = []
    for video in videos:
        row = video.model_dump(mode="json")
        # 作者话题通过带原文的字段传入，避免重复计入展示列表里的译文。
        topic_tags = set(video.published_topic_tags())
        row["tags"] = [tag for tag in video.tags_with_people_evidence() if tag not in topic_tags]
        results.append(row)
    return _json(results)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)
