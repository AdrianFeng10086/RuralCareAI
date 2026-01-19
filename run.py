# 便捷启动脚本：python run.py
from code.api_app import app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("code.api_app:app", host="127.0.0.1", port=8000, reload=True)
