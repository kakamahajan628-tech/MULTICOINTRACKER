import os
import uvicorn
from dotenv import load_dotenv

load_dotenv(".env.whale")
uvicorn.run(
    "whale_watcher.app:app",
    host=os.getenv("WHALE_HOST", "127.0.0.1"),
    port=int(os.getenv("PORT", "8002")),
    workers=1,
)
