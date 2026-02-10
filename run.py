# 便捷启动脚本：python run.py
from dotenv import load_dotenv
import os
# 加载.env文件
load_dotenv(os.path.join(os.path.dirname(__file__), "envs", ".env"))
from code.api_app import app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("code.api_app:app", host="127.0.0.1", port=8000, reload=True)
