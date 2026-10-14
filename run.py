import uvicorn

from app import config

if __name__ == "__main__":
    # One worker only: login state and download jobs live in this process's memory.
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, log_level="info")
