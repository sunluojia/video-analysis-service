import pytest

from video_analysis.modules.analysis.job.results import (
    CreatorTags,
    Listening,
    Recognition,
    Sound,
    TopicTranslation,
    merge_listening,
)


def test_tag_content_and_count_are_controlled_by_the_prompt():
    tags = ["#护肤", "护肤", "Makeup", "makeup", "美妆", "美妆"] + [
        f"标签{number}" for number in range(21)
    ]

    assert Recognition(tags=tags, summary="").tags == tags
    assert CreatorTags(tags=tags).tags == tags


def test_products_keep_sizes_and_their_sources():
    output = {
        "tags": ["卫衣大号"],
        "summary": "",
        "products": [
            {
                "name": "卫衣",
                "brand": "Acme",
                "original_name": "Acme Hoodie",
                "frame_indices": [1],
                "evidence": "吊牌上的品牌",
                "sizes": [{"value": "L", "transcript_quote": "我穿 L 码", "evidence": "口述尺码"}],
            }
        ],
    }

    [product] = Recognition.model_validate(output).model_dump(mode="json")["products"]

    assert product["frame_indices"] == [1]
    assert product["evidence"] == "吊牌上的品牌"
    assert product["sizes"] == [
        {"frame_indices": [], "transcript_quote": "我穿 L 码", "evidence": "口述尺码", "value": "L"}
    ]


def test_listening_is_merged_in_time_order_and_drops_silence_when_other_sounds_exist():
    merged = merge_listening(
        [
            Listening(sounds=[Sound.SILENCE]),
            Listening(sounds=[Sound.SPEECH, Sound.MUSIC], transcript="第一句"),
            Listening(sounds=[Sound.SPEECH], transcript="第二句"),
        ]
    )
    assert merged.sounds == [Sound.SPEECH, Sound.MUSIC]
    assert merged.transcript == "第一句\n第二句"
    assert merge_listening([]).sounds == []


@pytest.mark.parametrize(
    ("sounds", "sound_tags"),
    [
        ([Sound.MUSIC, Sound.SPEECH], ["口播"]),
        ([Sound.AMBIENT, Sound.MUSIC], ["纯音乐"]),
        ([Sound.AMBIENT], ["无口播"]),
        ([Sound.SILENCE], ["无口播"]),
        ([], []),
    ],
)
def test_display_tags_use_listening_and_keep_publisher_topics_after_content(sounds, sound_tags):
    raw_tags = ["#游戏", "纯音乐", "服装", "服装"]
    recognition = Recognition(
        tags=raw_tags,
        summary="",
        topic_translations=[
            TopicTranslation(topic="#game", translation="游戏"),
            TopicTranslation(topic="#outfit", translation="服装"),
            TopicTranslation(topic="#unknown", translation=""),
        ],
    )

    assert recognition.display_tags(Listening(sounds=sounds)) == [
        "服装",
        *sound_tags,
        "#游戏",
        "#服装",
    ]
    assert recognition.tags == raw_tags


def test_display_tags_join_recorded_sizes_to_their_own_products():
    recognition = Recognition.model_validate(
        {
            "tags": ["产品展示", "XL"],
            "summary": "",
            "products": [
                {
                    "name": "卫衣",
                    "evidence": "卫衣吊牌",
                    "sizes": [{"value": "XL", "evidence": "同一件卫衣的吊牌"}],
                },
                {
                    "name": "手套",
                    "evidence": "口述",
                    "sizes": [{"value": "medium", "evidence": "手套包装"}],
                },
            ],
        }
    )

    assert recognition.display_tags(Listening(sounds=[])) == [
        "产品展示",
        "卫衣",
        "卫衣XL码",
        "手套",
        "手套中号",
    ]
    assert [product.sizes[0].value for product in recognition.products] == ["XL", "medium"]


def test_display_projects_appearance_categories_with_independent_evidence():
    recognition = Recognition.model_validate(
        {
            "tags": ["人脸出镜"],
            "summary": "",
            "appearances": [
                {"frame_index": 1, "age_group": "25-34岁", "evidence": "清楚的正脸"},
                {"frame_index": 2, "skin_tone": "偏白肤色", "evidence": "可见手臂的色调"},
            ],
        }
    )

    assert recognition.display_tags(Listening(sounds=[])) == ["人脸出镜", "25-34岁", "偏白肤色"]


@pytest.mark.parametrize("closing_bracket", ["]", ""])
def test_recognition_decodes_lists_returned_as_strings(closing_bracket):
    recognition = Recognition.model_validate(
        {
            "tags": ["日常"],
            "summary": "",
            "appearances": '[{"frame_index": 0, "skin_tone": "白肤色", "evidence": "手臂可见"}]',
            "topic_translations": '[{"topic": "#coffee", "translation": "咖啡"}' + closing_bracket,
        }
    )

    assert recognition.appearance_tags() == ["白肤色"]
    assert recognition.published_topic_tags() == ["#咖啡"]


def face(age_group: str, skin_tone: str) -> Recognition:
    return Recognition.model_validate(
        {
            "tags": ["人脸出镜"],
            "summary": "",
            "appearances": [
                {
                    "frame_index": 0,
                    "age_group": age_group,
                    "skin_tone": skin_tone,
                    "evidence": "近景人脸",
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("age_group", "skin_tone", "tags"),
    [
        ("18-24岁", "偏黄肤色", ["人脸出镜", "18-24岁", "偏黄肤色"]),
        ("成年人", "无法判断肤色", ["人脸出镜"]),
    ],
)
def test_appearance_keeps_values_outside_the_list_of_the_same_kind(age_group, skin_tone, tags):
    assert face(age_group, skin_tone).display_tags(Listening(sounds=[])) == tags


def test_creator_keeps_only_appearance_values_seen_in_its_videos():
    summary = CreatorTags(tags=["健身", "偏黄肤色", "偏棕肤色", "30岁左右"])

    assert summary.with_observed_tags([face("18-24岁", "偏黄肤色")]).tags == [
        "健身",
        "偏黄肤色",
        "18-24岁",
    ]


def test_video_length_tag_comes_from_measured_duration():
    recognition = Recognition(tags=["生活记录", "视频时长8秒", "#美食"], summary="")

    assert recognition.display_tags(Listening(sounds=[]), duration_seconds=15.44) == [
        "生活记录",
        "视频时长15.4秒",
        "#美食",
    ]
    assert recognition.display_tags(Listening(sounds=[])) == ["生活记录", "#美食"]
