# 益心守护（SFBT + 生成式 AI）

面向乡村困境儿童的心理支持平台，包含管理后台与用户端聊天。支持知识库上传与检索、危机提示、游客模式（不保存记录）。

## 功能概览
- 用户端聊天：SFBT 对话流程、流式回复。
- 数字人交互：用户端聊天页集成 Live2D 数字人（动作触发、点击互动、可选语音朗读）。
- 管理后台：儿童档案、知识库管理、心理预警。
- 知识库：上传 PDF 文档并构建向量索引。
- 游客模式：无需账号进入对话，不保存任何对话与记录。

## 目录结构
- src/ 后端主逻辑
- templates/ 前端页面模板
- static/ 样式与静态资源
- uploads/knowledge/ 知识库上传文件目录

## 环境与依赖
建议使用 Python 3.9+。

安装依赖：

```bash
pip install -r requirements.txt
```

## 启动方式
项目已提供启动脚本：

```bash
python run.py
```

默认启动地址：
- 用户端：http://127.0.0.1:8000/
- 管理端：http://127.0.0.1:8000/admin

## 数字人（Live2D）说明
- Live2D 模型目录：`hiyori_free_zh/`
- 模型入口文件：`hiyori_free_zh/runtime/hiyori_free_t08.model3.json`
- 前端页面：`/user/chat`

说明：
- 后端会自动挂载 `/live2d` 静态路径，前端通过 `/live2d/runtime/hiyori_free_t08.model3.json` 加载模型。
- 页面默认启用数字人动作与点击互动；可用“回复朗读”开关控制浏览器语音播报。
- 若模型不显示，先检查模型目录是否存在，再确认网络可访问前端依赖 CDN。

## 环境变量
可在 envs/.env 中配置（示例）：
- DEEPSEEK_API_URL：模型 API 地址
- DEEPSEEK_API：API Key
- API_MODEL：模型名称
- TEMPERATURE：生成温度
- API_NUM_CTX：上下文长度
- API_MAX_TOKENS：最大输出

## 游客模式
登录页提供“游客登陆”入口：
- 进入后可正常对话
- 不保存对话、不创建儿童记录

## 知识库上传与同步
- 上传入口：管理端 -> 上传知识
- 上传后会自动写入数据库并重建向量库
- uploads/knowledge 下新增/移除文件会自动同步（页面轮询或启动时）

## 账号与权限
- 管理端账号密码可在 src/auth.py 中配置或通过环境变量覆盖
- 用户端账号由管理员在后台创建

## 常见问题
- 上传后未生效：检查 uploads/knowledge 目录与向量库构建日志
- 无法访问管理端：确认管理员账号密码是否正确配置

## 许可证
本项目未附带许可证文件，如需开源发布请补充 LICENSE。
