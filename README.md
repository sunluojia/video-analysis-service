# 视频分析服务

接收[调度中心](https://github.com/sunluojia/video-scheduler-center)的单视频分析和作者汇总任务，完成下载校验、抽帧、听音、识别和汇总，按阶段回传结果。不提供页面，只提供 API。设计和接口约定见 [docs/architecture.md](docs/architecture.md)。

## 运行角色

| 进程 | 命令 | 职责 |
| --- | --- | --- |
| API | `video-analysis-api` | 本服务的 HTTP 接口，要求服务凭据；启动后向调度中心注册并保持心跳 |
| 任务接收 | `dramatiq video_analysis.entrypoints.consumer:broker` | 从 Redis 队列接收任务并保存 |
| Worker | `video-analysis-worker` | 下载、媒体处理与 AI 调用，投递回传事件，清理过期文件 |

三种进程都可以启动多个；API 和 Worker 需要共享工作目录。

## 本地开发

需要 Python 3.12、PostgreSQL、Redis 和 FFmpeg。

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
```

在 `.env` 中设置 `VA_SERVICE_TOKEN`（至少 16 位，与调度中心一致，API 和 Worker 启动时需要）、`VA_SCHEDULER_URL` 和 `VA_PUBLIC_URL`。Worker 默认每个视频抽取 6 帧，可通过 `VA_FRAME_COUNT` 调整。

代码按“业务域／模块”组织，例如 `modules/analysis/job/`、`modules/ai/prompt/`，模块内按 `models`、`tables`、`repository`、`service`、`schemas`、`router` 分文件；公共基础在 `core/`，外部系统接入在 `integrations/`。完整结构见设计文档第 6 节。

项目根目录的 `.mcp.json` 连接本机 PyCharm 的 MCP 服务（`http://127.0.0.1:64462/stream`），需要 PyCharm 打开本项目并启用 MCP 服务。

## 测试

```bash
pytest
```

数据库相关测试需要一个独立的测试库，测试会重建其中的表。可以用 Docker 启动一个（映射到 55432，避开本机已有的 PostgreSQL）：

```bash
docker run -d --name video-analysis-test-db -e POSTGRES_USER=video -e POSTGRES_PASSWORD=video -e POSTGRES_DB=video_analysis_test -p 55432:5432 postgres:17
```

```bash
VA_TEST_DATABASE_URL=postgresql+asyncpg://video:video@localhost:55432/video_analysis_test pytest
```

PowerShell 中先执行 `$env:VA_TEST_DATABASE_URL = "postgresql+asyncpg://video:video@localhost:55432/video_analysis_test"`，再运行 `pytest`。之后用 `docker start video-analysis-test-db` 重新启动即可。队列测试使用 Dramatiq 的 StubBroker，不需要 Redis。媒体测试在安装了 FFmpeg 时运行。

## 部署

```bash
docker compose -f deployment/compose.yaml up -d --build
```

compose 自带 PostgreSQL 和 Redis，便于单独运行；和调度中心一起部署时，把 `deployment/compose.yaml` 里的 `VA_REDIS_URL` 改为调度中心使用的 Redis。compose 里写的数据库和 Redis 地址会覆盖 `.env`（`.env` 里是源码运行用的本机地址），改 `.env` 不起作用。API 发布在 8000 端口供调度中心经局域网访问，`VA_PUBLIC_URL` 设为调度中心能访问到的地址。

## 首次使用

经调度中心的 AI 配置页面（或直接调用 `/api/ai/*` 接口）：

1. 添加供应商（LangChain 供应商标识和密钥环境变量名）、模型及其并发额度。
2. 为听音、视频识别、作者汇总配置路由和目标份额。没有路由时不开始新视频。
3. 按需调整各环节的系统提示词；不调整时使用内置提示词。

### 百炼 Prompt Cache

百炼支持隐式缓存的模型会自动尝试复用公共前缀。用 Chat Completions 接口调用支持显式缓存的百炼模型（如 `qwen3.6-plus`、`qwen3.8-flash`，完整列表见 `EXPLICIT_CACHE_MODELS`）时，默认开启显式缓存：系统提示词带 `cache_control`，工具定义随系统提示词参与缓存，业务数据和媒体位于后续消息。要关掉，在调度中心「AI 配置 → 模型 → 编辑」关闭「显式 Prompt Cache」；直接调用配置接口时，在模型的 `params` 顶层设置 `"bailian_explicit_cache": false`。

显式缓存的公共前缀至少需要 1024 个 token，有效期为 5 分钟，命中会续期。创建缓存按输入单价的 125% 计费，命中部分按 10% 计费，适合短时间内反复使用同一版长提示词的任务。[百炼上下文缓存](https://help.aliyun.com/zh/model-studio/context-cache)

每次模型调用的输入、输出和命中缓存的 token 记在 `ai_calls` 里。`GET /api/ai/usage` 按时段、环节和模型汇总调用次数、成败、token、缓存命中和调用总耗时，调度中心的数据看板和「AI 配置 → 模型」的“缓存命中（24 小时）”列都用它。命中率 = 命中的 token ÷ 报告了命中数的调用的输入 token；服务商没报告命中数的调用不计入。Worker 的 `prompt_cache_usage` 日志仍逐条记录模型、结果结构、提示词版本、缓存模式、`prompt_tokens` 和 `cached_tokens`。
