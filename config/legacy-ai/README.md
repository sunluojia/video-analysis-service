# 旧项目 AI 配置与提示词迁移

本目录将旧项目 `video-insight-pipeline` 和 `video-recognition-engine` 的默认配置迁移为当前服务可用的参考模板。

| 新阶段 | 模型 | 生效提示词来源 |
| --- | --- | --- |
| 听音 listen | qwen3.8-omni-flash | 旧 CloudSoundClassifier.PROMPT，转换为 sounds/transcript |
| 视频识别 recognize | qwen3.8-flash | CLOUD_SYSTEM_PROMPT、VISUAL_LABEL_RULES、CONTENT_LABEL_RULES、属性与商品规则及清理后的候选词典 |
| 作者汇总 summarize | qwen3.8-flash | type_summary.zh.txt 的19条业务规则，转换为 tags 输出 |

本目录的 `listen.txt`、`recognize.txt` 和 `summarize.txt` 是迁移后的可编辑文本。`source/` 保存原始提示词、候选词典与政策快照，`defaults.json` 保存供应商、模型、路由与旧参数对照。

可在调度中心的 AI 配置页面设置模型路由并启用对应的提示词版本。百炼密钥通过分析服务的 `DASHSCOPE_API_KEY` 环境变量提供。

## 已适配的差异

- 听音从旧版 speech/singing/music/evidence/speech_quotes 转为当前 sounds/transcript；保留轻声讲话、歌词与讲话区分、环境声音及原语言转写规则。
- 识别从旧版 observations/matched_tags/attributes 等复杂结构转为 tags/products/summary/topic_translations；保留证据边界、中文命名、商品类别、人物与动物出镜、声音、主题和话题翻译规则。候选词典只提供中文名称，不复制内部编号。
- 作者汇总保留19条语义规则，删除旧接口专用的 source_ids、groups、dropped 输出要求。发布话题翻译与识别标签分开传入汇总，商品尺码与证据使用当前 Product/ProductSize 字段。原逐帧人物属性对象和汇总来源分组仍无对应结果字段，原文留存在 source/。
- 旧语义别名、占位过滤、商品命名、声音分类和展示标签生成规则已转为可编辑提示词。归档汇总代码中与后续规则冲突的猫狗合并、盲盒归玩具、品牌拼接等行为不采用；词典的机械子串删除改为按实际语义判断。
- 新代码已移除标签去#、大小写合并和20项截断，原样保存模型输出。命名、过滤、归并、#形式和数量由生效提示词决定；字段类型、必填结构仍由 Pydantic 与 LangChain Structured Output 校验。格式正确不能证明语义正确，真实效果待样本验证。
- 提示词不再重复手写JSON模板。当前通过 `with_structured_output(schema, method="function_calling")` 传递契约；原生JSON Schema与JSON Mode的支持依赖供应商，不能仅凭接口地址兼容就假定全部支持。参见 [LangChain说明](https://docs.langchain.com/oss/python/langchain/models#structured-output)。
- 识别和汇总共享一个视觉模型记录及其额度。当前每模型只能保存一套参数，因此共同使用 temperature=0、max_tokens=16000；旧识别 temperature=0.1 和旧汇总 max_tokens=6000 仅保留在对照记录中。
- 并发4、RPM60采用新服务默认值，属于预览起始配置；旧全局实时并发30、独立汇总并发4不等同于新模型池配额，未直接照搬。
- 旧独立 ASR 模型仅留作参考，没有新增调用阶段；抽帧和音频分段仍使用当前服务设置。

配置校验只覆盖数据库写入、页面接口和离线请求构造，没有调用付费模型，也不表示识别质量已验证。

阿里云当前的[模型说明](https://help.aliyun.com/en/model-studio/qwen3-8-omni-flash)列出了该听音模型的音频输入与 Function Calling 能力；实际账户权限、限额和返回效果仍需补充密钥后验证。
