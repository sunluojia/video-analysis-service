"""模型输出的分析结果，以及结果校验、声音分类和合并规则。"""

import json
import re
import unicodedata
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class Sound(StrEnum):
    SPEECH = "speech"
    MUSIC = "music"
    AMBIENT = "ambient"
    SILENCE = "silence"


_SOUND_LABELS = {
    Sound.SPEECH: "口播",
    Sound.MUSIC: "纯音乐",
    Sound.AMBIENT: "无口播",
    Sound.SILENCE: "无口播",
}
_SIZE_WORDS = {
    "small": "小号",
    "medium": "中号",
    "large": "大号",
    "extra small": "加小号",
    "extra large": "加大号",
    "one size": "均码",
    "onesize": "均码",
}


def _size_label(value: str) -> str:
    """只改显示形式，结构化尺码保留原文和制式。"""
    text = unicodedata.normalize("NFKC", value).strip()
    token = re.sub(r"[-_\s]+", " ", text).casefold()
    token = re.sub(r"^(?:size|尺码)\s*:?\s*", "", token)
    if token in _SIZE_WORDS:
        return _SIZE_WORDS[token]
    code = token.replace(" ", "").removesuffix("码")
    if re.fullmatch(r"[xsml]|x{1,4}[sl]|[2-6]x[sl]|\d{2,3}[a-j]{1,2}", code):
        return f"{code.upper()}码"
    if re.fullmatch(r"(?:加小|小|中|大|加大|特大)号|均码|\d{1,3}码", token):
        return token
    return ""


class Listening(BaseModel):
    sounds: list[Sound] = Field(
        description="出现的声音类型：speech 口播或讲话，music 音乐，ambient 环境声，silence 静音"
    )
    transcript: str = Field(default="", description="讲话内容原文，没有讲话时为空")

    @field_validator("sounds", mode="before")
    @classmethod
    def known_sounds(cls, value: object) -> object:
        """认不出的声音类型丢掉，不让整段听音失败。"""
        return [item for item in value if item in Sound] if isinstance(value, list) else value


def merge_listening(parts: Sequence[Listening]) -> Listening:
    """按时间顺序合并各音频段；只要有其他声音，就不再标记为静音。无音频时结果为空。"""
    sounds = list(dict.fromkeys(sound for part in parts for sound in part.sounds))
    if len(sounds) > 1 and Sound.SILENCE in sounds:
        sounds.remove(Sound.SILENCE)
    transcript = "\n".join(part.transcript for part in parts if part.transcript)
    return Listening(sounds=sounds, transcript=transcript)


class Sourced(BaseModel):
    """模型给出的判断及其来源，供人工核对。"""

    frame_indices: list[int] = Field(
        default_factory=list, description="依据所在的图片序号，从 0 开始"
    )
    transcript_quote: str = Field(
        default="", description="作为依据的讲话原文，逐字摘录，没有则为空"
    )
    evidence: str = Field(description="简要说明判断依据")


class ProductSize(Sourced):
    value: str = Field(
        description="包装、吊牌文字或口述中与该商品明确对应的原始尺码标记，例如 XL、large，不翻译"
    )


class Product(Sourced):
    name: str = Field(description="有直接依据的简短中文品类，不含品牌、型号、颜色等修饰")
    category: str = Field(default="", description="商品品类，与 name 相同")
    brand: str = Field(default="", description="证据中的原始品牌拼写，无法确认时为空")
    original_name: str = Field(default="", description="完整的中英文品名原文")
    sizes: list[ProductSize] = Field(
        default_factory=list, description="与该商品明确对应的尺码，不从人物外观推测"
    )


class TopicTranslation(BaseModel):
    topic: str = Field(description="发布话题原文")
    translation: str = Field(description="话题的中文翻译")


class AgeGroup(StrEnum):
    UP_TO_24 = "24岁及以下"
    FROM_25_TO_34 = "25-34岁"
    FROM_35_TO_44 = "35-44岁"
    FROM_45_TO_54 = "45-54岁"
    FROM_55 = "55岁+"


class SkinTone(StrEnum):
    VERY_FAIR = "极白肤色"
    LIGHT_FAIR = "偏白肤色"
    FAIR = "白肤色"
    FAIR_YELLOW = "白中偏黄肤色"
    LIGHT_YELLOW = "浅黄肤色"
    YELLOW = "黄肤色"
    DEEP_YELLOW = "深黄肤色"
    LIGHT_DARK = "浅黑肤色"
    DARK = "偏黑肤色"
    DEEP_DARK = "深黑肤色"
    NEUTRAL = "中性肤色"


_LISTED_APPEARANCE_TAGS = frozenset([*AgeGroup, *SkinTone])
"""早期结果把年龄段和肤色直接写在标签里，靠这个列表认出来，汇总时一并带上。"""
DURATION_PREFIX = "视频时长"
"""不统计时长：模型给出的时长标签一律丢掉。"""


class Appearance(BaseModel):
    frame_index: int = Field(description="这项观察的画面序号，从 0 开始")
    age_group: str | None = Field(
        None,
        description=f"清晰人脸支持的外观年龄段，优先用 {'、'.join(AgeGroup)}，无法区分时为 null",
    )
    skin_tone: str | None = Field(
        None,
        description=f"画面中清楚可见的皮肤色调，优先用 {'、'.join(SkinTone)}，"
        "有严重色偏或证据不足时为 null",
    )
    evidence: str = Field(default="", description="简述帧内人物位置和可见依据")

    @field_validator("age_group", "skin_tone", mode="before")
    @classmethod
    def blank_as_none(cls, value: object) -> object:
        """模型写什么都保留，空白当作没给出。"""
        return (value.strip() or None) if isinstance(value, str) else value


class Recognition(BaseModel):
    tags: list[str] = Field(description="依据画面和声音事实得出的视频内容标签，不照搬发布话题")
    appearances: list[Appearance] = Field(
        default_factory=list, description="单独核查肤色和外观年龄段，保留有据的帧，无依据时为空"
    )
    products: list[Product] = Field(
        default_factory=list, description="视频中清晰出现或口播推荐的商品"
    )
    summary: str = Field(description="视频内容摘要")
    topic_translations: list[TopicTranslation] = Field(
        default_factory=list, description="发布话题的翻译，与识别标签分开"
    )

    @field_validator("appearances", "topic_translations", mode="before")
    @classmethod
    def decode_list_argument(cls, value: object) -> object:
        """兼容模型把列表写成 JSON 字符串，或在最后一个完整对象后漏写右方括号。"""
        if not isinstance(value, str) or not (text := value.strip()).startswith("["):
            return value
        if text.endswith("}"):
            text += "]"
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value

    def published_topic_tags(self) -> list[str]:
        translations = (
            item.translation.strip().lstrip("#＃").strip() for item in self.topic_translations
        )
        return [f"#{text}" for text in translations if text]

    def appearance_tags(self) -> list[str]:
        tags = [tag for tag in self.tags if tag in _LISTED_APPEARANCE_TAGS]
        tags.extend(
            value
            for observation in self.appearances
            for value in (observation.age_group, observation.skin_tone)
            if value is not None
        )
        return list(dict.fromkeys(tags))

    def display_tags(self, listening: Listening) -> list[str]:
        """使用独立听音结论，作者话题放在最后。"""
        tags = []
        topics = []
        for tag in self.tags:
            if tag.startswith(("#", "＃")):
                topics.append(tag)
            elif (
                tag not in _SOUND_LABELS.values()
                and not _size_label(tag)
                and not tag.startswith(DURATION_PREFIX)
            ):
                tags.append(tag)
        tags.extend(self.appearance_tags())
        for product in self.products:
            if name := product.name.strip():
                tags.append(name)
                for size in product.sizes:
                    if label := _size_label(size.value):
                        tags.append(f"{name}{label}")
        for sound, label in _SOUND_LABELS.items():
            if sound in listening.sounds:
                tags.append(label)
                break
        return list(dict.fromkeys([*tags, *topics, *self.published_topic_tags()]))


class CreatorTags(BaseModel):
    tags: list[str] = Field(description="对各视频标签语义去重后的创作者标签")

    def with_observed_tags(self, videos: Sequence[Recognition]) -> "CreatorTags":
        """补上视频里观察到、汇总漏掉的年龄段和肤色；去掉时长标签。"""
        tags = [tag for tag in self.tags if not tag.startswith(DURATION_PREFIX)]
        tags.extend(tag for video in videos for tag in video.appearance_tags())
        return self.model_copy(update={"tags": tags})
