"""Start FastAPI (Web + Admin + Payment webhook)."""
import os
import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("WEBHOOK_PORT") or "8080")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )
