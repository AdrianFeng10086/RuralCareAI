# YiXin Guardian (SFBT + Generative AI)

A psychological support platform for rural children in difficult circumstances, including admin backend and user chat interface. Supports knowledge base upload and retrieval, crisis alerts, and guest mode (no record saving).

## Feature Overview
- User chat: SFBT dialogue flow with streaming responses
- Digital avatar interaction: User chat page integrates Live2D digital avatar (action triggers, click interactions, optional voice reading)
- Admin backend: Children's profiles, knowledge base management, psychological early warning
- Knowledge base: Upload PDF documents and build vector index
- Guest mode: Enter dialogue without account, no saving of conversations or records

## Directory Structure
- src/ Backend main logic
- templates/ Frontend page templates
- static/ Styles and static resources
- uploads/knowledge/ Knowledge base upload file directory

## Environment & Dependencies
Python 3.9+ recommended.

Install dependencies:

```bash
pip install -r requirements.txt
```

## Launch Method
Project provides startup script:

```bash
python run.py
```

Default launch addresses:
- User interface: http://127.0.0.1:8000/
- Admin interface: http://127.0.0.1:8000/admin

## Digital Avatar (Live2D) Description
- Live2D model directory: `hiyori_free_zh/`
- Model entry file: `hiyori_free_zh/runtime/hiyori_free_t08.model3.json`
- Frontend page: `/user/chat`

Instructions:
- Backend automatically mounts `/live2d` static path, frontend loads model via `/live2d/runtime/hiyori_free_t08.model3.json`
- Page enables digital avatar actions and click interactions by default; "Reply Reading" switch controls browser voice playback
- If model doesn't display, first check if model directory exists, then confirm network can access frontend dependency CDN

## Environment Variables
Can be configured in envs/.env (example):
- DEEPSEEK_API_URL: Model API address
- DEEPSEEK_API: API Key
- API_MODEL: Model name
- TEMPERATURE: Generation temperature
- API_NUM_CTX: Context length
- API_MAX_TOKENS: Maximum output

## Guest Mode
Login page provides "Guest Login" entry:
- Can chat normally after entering
- Does not save conversations or create children records

## Knowledge Base Upload & Sync
- Upload entry: Admin interface -> Upload Knowledge
- After upload, automatically writes to database and rebuilds vector library
- Files added/removed in uploads/knowledge automatically sync (page polling or at startup)

## Account & Permissions
- Admin account password can be configured in src/auth.py or overridden via environment variables
- User accounts created by administrators in backend

## Common Issues
- Upload not taking effect: Check uploads/knowledge directory and vector library build logs
- Cannot access admin interface: Confirm admin account password is correctly configured

## License
This project does not include a license file. If open-source release is needed, please add LICENSE.
