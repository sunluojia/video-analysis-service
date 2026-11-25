import pytest

from video_analysis.modules.analysis.job.results import (
    AgeGroup,
    CreatorTags,
    Listening,
    Recognition,
    SkinTone,
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


def test_listed_values_in_old_tags_count_as_appearance():
    listed = [*AgeGroup, *SkinTone]

    assert Recognition(tags=["美妆", *listed], summary="").appearance_tags() == listed


@pytest.mark.parametrize("prefix", ["", " ", "\t\u3000"])
def test_duration_is_not_a_tag(prefix):
    recognition = Recognition(tags=["生活记录", f"{prefix}视频时长8秒", "#美食"], summary="")

    assert recognition.display_tags(Listening(sounds=[])) == ["生活记录", "#美食"]
    summary = CreatorTags(tags=["生活记录", f"{prefix}视频时长8–31秒"])
    assert summary.with_observed_tags([recognition]).tags == ["生活记录"]


@pytest.mark.parametrize(
    "invalid_tag",
    [
        "无明显美甲",
        "没有美甲",
        "没有滤镜",
        "无人脸出镜",
        "无明显妆容",
        "无妆容",
        "无宠物",
        "无小孩",
        "采样未见宠物",
        "采样未见小孩",
        "未见健身",
        "不确定是儿童",
        "无法判断肤色",
        " 未知妆容 ",
        "疑似宠物",
        "#没有美甲",
    ],
)
def test_negative_and_uncertain_tags_are_removed_even_without_a_conflict(invalid_tag):
    video = Recognition(tags=[invalid_tag, "无人出镜"], summary="")
    assert video.display_tags(Listening(sounds=[Sound.AMBIENT])) == ["无人出镜", "无口播"]
    assert CreatorTags(tags=[invalid_tag, "无人出镜", "无口播"]).with_observed_tags(
        [video]
    ).tags == ["无人出镜", "无口播"]


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (["肢体出镜", "1人出镜"], ["仅肢体出镜", "1人出镜"]),
        (["仅肢体出镜", "人脸出镜", "肢体出镜"], ["人脸出镜"]),
        (["人脸出镜", "仅肢体出镜", "2人出镜"], ["人脸出镜", "2人出镜"]),
    ],
)
def test_video_prefers_face_over_body_only(tags, expected):
    assert Recognition(tags=tags, summary="").display_tags(Listening(sounds=[])) == expected


def test_summary_keeps_both_face_and_body_and_drops_negative_sides_of_conflicts():
    videos = [
        Recognition(tags=["肢体出镜", "无明显美甲", "无宠物"], summary=""),
        Recognition(tags=["人脸出镜", "有美甲", "有宠物"], summary=""),
    ]
    summary = CreatorTags(
        tags=[
            "肢体出镜",
            "仅肢体出镜",
            "人脸出镜",
            "有美甲",
            "无明显美甲",
            "没有美甲",
            "没有宠物",
            "有宠物",
            "无宠物",
        ]
    )

    assert summary.with_observed_tags(videos).tags == ["仅肢体出镜", "人脸出镜", "有美甲", "有宠物"]


def test_pet_and_child_observations_use_allowed_tags_without_requiring_a_face():
    observations = [
        "仅肢体出镜",
        "2人出镜",
        "有小孩",
        "1个小孩",
        "有宠物",
        "金毛寻回犬外观",
        "1只宠物",
    ]
    video = Recognition(
        tags=[*observations, "婴幼儿出镜", "儿童出镜", "动物出镜", "宠物出镜", "上半身出镜"],
        summary="成人怀中的婴儿脸部被遮住，能看清身体。",
    )

    assert video.display_tags(Listening(sounds=[])) == observations
    assert CreatorTags(tags=video.tags).with_observed_tags([video]).tags == observations


def test_child_age_categories_and_allowed_people_counts_remain_distinct():
    ages = ["婴幼儿", "幼儿", "儿童", "青少年"]
    counts = ["1人出镜", "2人出镜", "12人出镜", "多人出镜"]
    video = Recognition(tags=["有小孩", *ages, "多人出镜"], summary="")
    summary = CreatorTags(tags=["有小孩", *ages, *counts, "0人出镜", "双人出镜"])

    assert video.display_tags(Listening(sounds=[])) == ["有小孩", *ages, "多人出镜"]
    assert summary.with_observed_tags([video]).tags == ["有小孩", *ages, *counts]


def test_gender_aliases_merge_without_changing_skin_tone_or_related_content():
    video = Recognition(
        tags=["女性呈现", "男性呈现", "中性呈现", "女性", "中性肤色", "中性穿搭"], summary=""
    )
    expected = ["女性", "男性", "中性", "中性穿搭", "中性肤色"]

    assert video.display_tags(Listening(sounds=[])) == expected
    assert (
        CreatorTags(tags=["女性呈现", "男性呈现", "中性呈现", "中性穿搭"])
        .with_observed_tags([video])
        .tags
        == expected
    )


@pytest.mark.parametrize("prefix", ["其它", "其他"])
def test_generic_scene_labels_drop_and_specific_scenes_remain(prefix):
    tags = [prefix, f"{prefix}室内", f"{prefix}室外", f"{prefix}户外", "自然户外", "客厅"]
    video = Recognition(tags=tags, summary="")

    assert video.display_tags(Listening(sounds=[])) == ["自然户外", "客厅"]
    assert CreatorTags(tags=tags).with_observed_tags([video]).tags == ["自然户外", "客厅"]


@pytest.mark.parametrize("identity", ["拉丁裔", "#拉丁裔", "＃拉丁裔", "#宝妈"])
def test_summary_preserves_identity_from_the_model_without_appending_published_topics(identity):
    video = Recognition(
        tags=["#热门", "#亚裔", "#好物分享"],
        summary="",
        topic_translations=[
            TopicTranslation(topic="#latina", translation="拉丁裔"),
            TopicTranslation(topic="#fyp", translation="推荐"),
            TopicTranslation(topic="#TikTokShop", translation="TikTok商店"),
        ],
    )

    assert "#拉丁裔" in video.display_tags(Listening(sounds=[]))
    assert CreatorTags(tags=["生活记录", identity, identity]).with_observed_tags([video]).tags == [
        "生活记录",
        identity,
    ]
