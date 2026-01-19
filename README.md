# 益心守护——基于SFBT与生成式AI的乡村困境儿童心理支持平台

一个基于 FastAPI 的简易管理后台与用户聊天前端，用于支持困难儿童的在线陪伴与建议生成。集成：
- 对话引擎：Ollama（可选），或本地“模拟模式”快速演示
- RAG：本地知识库（向量检索）与在线检索（Bing/Baidu SERP 片段）
- 实时预警：对自伤/暴力等敏感内容进行危机信号检测，SSE 实时推送到后台页面
- 管理后台：儿童档案、会话管理、知识库上传/重建

---

## 目录结构

```
.
├─ code/                  # 新版正式代码（主包）
│  ├─ api_app.py          # FastAPI 应用（入口 app）
│  ├─ dialogue_manager_ollama.py  # 对话编排与安全策略
│  ├─ rag_module.py       # RAG：在线检索 + 本地向量库
│  ├─ db_models.py        # SQLAlchemy ORM 模型与会话
│  ├─ alert_bus.py        # 进程内 pub/sub，用于 SSE 推送
│  ├─ auth.py             # 管理端认证中间件与依赖
│  ├─ migrate_add_conversations.py # 历史迁移脚本
│  ├─ seed_sfbt_data.py   # 示例种子（可选）
│  └─ __init__.py
├─ templates/             # Jinja2 模板（页面）
├─ static/                # 静态资源（CSS 等）
├─ uploads/knowledge/     # 后台上传的知识文件（PDF 等）
├─ run.py                 # 便捷启动脚本（推荐）
├─ requirements.txt       # 依赖
└─ sfbt_ollama.db         # SQLite 数据库（首次运行自动生成）
```

---

## 快速开始（Windows PowerShell）

1) 准备 Python 环境（建议 3.10+）

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

2) 选择运行模式

- 模拟模式（无需 Ollama，前后端联调/演示用）：
  - 设置环境变量 `MOCK_LLM=1` 后启动，模型回复为可控的本地模拟。
- 真机模式（使用 Ollama）：
  - 安装并启动 Ollama，准备模型（默认 deepseek-r1:7b，可改）。
  - 如 Ollama 不在本机默认端口，设置 `OLLAMA_HOST`。

3) 启动服务（推荐）

```powershell
python run.py
```

或使用 Uvicorn：

```powershell
uvicorn code.api_app:app --reload --host 127.0.0.1 --port 8000
```

4) 访问地址
- 用户端聊天页：http://127.0.0.1:8000/
- 管理后台登录：http://127.0.0.1:8000/admin/login

默认后台账号（请尽快修改环境变量）：
- 用户名：root
- 密码：root

---

## 核心功能

- 儿童档案：添加、编辑、删除；自动推进“阶段”字段以反映陪伴进程
- 会话管理：面向每个儿童维护独立会话；提供会话历史与按名称查询
- 对话生成：
  - 严格模板模式（默认）：5 段结构（共情、肯定、探索、编号行动、鼓励），带格式校验与重试
  - 宽松模式：更自然的建议风格（通过 `STRICT_TEMPLATE=0` 切换）
  - 解释模式：返回 JSON，包含 answer 与 explanation（推理要点摘要）
- RAG：
  - 本地向量库：后台上传 PDF 后自动重建；使用 HuggingFace Embeddings 或哈希嵌入兜底
  - 在线检索：默认基于 Bing/Baidu 的 SERP 片段，不抓取正文（可禁用/切换）
- 实时预警：对自伤/暴力/受虐等关键词命中时写入 `CrisisAlert`，并通过 SSE 推到后台“预警列表”页（带心跳）

---

## 环境变量（节选）

- 认证
  - `ADMIN_USERNAME`（默认 root）
  - `ADMIN_PASSWORD`（默认 root）
- 模型 / 推理
  - `OLLAMA_MODEL`（默认 deepseek-r1:7b）
  - `OLLAMA_HOST`（例如 http://127.0.0.1:11434）
  - `OLLAMA_TEMPERATURE`（默认 0.5）
  - `OLLAMA_NUM_CTX`、`OLLAMA_NUM_PREDICT`（上下文与输出长度）
  - `MOCK_LLM`（1 启用模拟模式，无需 Ollama）
  - `STRICT_TEMPLATE`（1 严格五段式；0 宽松）
- RAG（本地/在线）
  - `ENABLE_WEB_RETRIEVAL_DEFAULT`（1 默认启用在线检索）
  - `WEB_RETRIEVAL_TOP_K`、`WEB_RETRIEVAL_PAGES`、`WEB_RETRIEVAL_TIMEOUT`、`WEB_PREFER_BAIDU`
  - `SERP_ONLY`（1 仅用 SERP 片段，不抓正文）
  - `CN_ONLY`（1 仅保留国内常见可访问域名）
  - `WEB_RELEVANCE_FILTER`（1 启用关键词相关性过滤）
  - `HF_ENDPOINT`/`HF_MIRROR`（国内镜像）
  - `HF_EMBEDDINGS_LOCAL_ONLY`（1 仅本地缓存，离线可用）
- 其它
  - `DIALOGUE_HISTORY_ROUNDS`（多轮上下文轮数，默认 4）
  - `MAX_CONTEXT_CHARS`（拼接外部/本地检索片段的最大字符数，默认 1800）

在 PowerShell 中可临时设置环境变量：

```powershell
$env:MOCK_LLM = "1"           # 示例：启用模拟模式
$env:STRICT_TEMPLATE = "1"    # 严格五段式
$env:ENABLE_WEB_RETRIEVAL_DEFAULT = "1"
```

---

## 知识库与向量检索

- 后台上传：管理后台 → 知识库 → 上传文件（建议 PDF）。系统会尝试解析文本并写入数据库。
- 重建向量库：上传后自动触发；也可在“知识库页”点击按钮，调用 `/admin/build_vectorstore`。
- Embeddings：优先 HuggingFace（可镜像/离线），失败则回退到哈希嵌入，保证功能不阻塞。

> 提示：早期 CSV 语料库功能已删除，不再出现在 UI 或代码中。

---

## 在线检索（Web）

- 默认仅使用 SERP 摘要（`SERP_ONLY=1`），不会抓取正文以提高稳定性与合规性。
- 可选过滤：`CN_ONLY=1` 保留国内域；`WEB_RELEVANCE_FILTER=1` 基于关键词相关性过滤。
- 关键参数可通过用户端开关控制（前端复选框）或环境变量默认值统一配置。

---

## 实时预警（SSE）

- 触发：对话中命中危机关键词时写入 `crisis_alerts` 表，并将警报事件发布到进程内总线。
- 后台页面订阅：`/admin/alerts/stream`，通过 EventSource 实时接收。
- 后台列表：`/admin/alerts` 可检索与标记“已处理”。

---

## 调试与排错

- `ollama_errors.log`：记录模型调用异常与返回摘要。
- `dialogue_messages.log`：设置 `DEBUG_DIALOGUE=1` 时，写入发送给模型的消息序列，用于多轮上下文排查。
- 若无 Ollama 环境，建议先用 `MOCK_LLM=1` 验证前后端流程。

---

## 安全与合规建议

- 尽快修改后台默认账号（`ADMIN_USERNAME`/`ADMIN_PASSWORD`）。
- 若部署到公网，建议使用反向代理 + HTTPS，并考虑独立的身份认证系统与审计日志。
- 危机预警仅作提醒，不构成诊断或紧急响应；页面中已加入必要的伦理与转介提示。

---

## 维护脚本与数据

- 迁移：`python -m code.migrate_add_conversations`（首次迁移会备份数据库到 `sfbt_ollama.db.bak`）
- 种子：`python -m code.seed_sfbt_data`
- 数据库：SQLite 文件位于项目根目录 `sfbt_ollama.db`；模型会尝试在启动时确保新表字段存在。

---

## 常见问题（FAQ）

1. 没有 Ollama 能跑吗？
   - 可以。设置 `MOCK_LLM=1` 即可使用“模拟模式”。

2. Embedding 模型下载失败怎么办？
   - 设置镜像 `HF_ENDPOINT` 或 `HF_MIRROR`，或将 `HF_EMBEDDINGS_LOCAL_ONLY=1` 以离线使用已有缓存；系统会在失败时自动回退到哈希嵌入。

3. 在线检索总是失败？
   - 可能被目标站点风控或网络限制；系统会优先用 SERP 摘要，并在失败时自动跳过在线检索，仅用本地知识继续。

4. 预警为什么没实时出现？
   - 检查后台是否打开了“预警列表”页（它会发起 SSE 订阅）；
   - 确认浏览器/代理没有拦截 EventSource。

---

## 开发小贴士

- 入口：`uvicorn code.api_app:app --reload` 或 `python run.py`
- 模板/静态资源路径：`code/api_app.py` 使用项目根目录的 `templates/` 与 `static/`
- 旧源码备份在 `legacy_src/`，请勿直接修改，改动都应在 `code/` 中进行
