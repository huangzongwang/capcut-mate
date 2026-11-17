"""Compatibility entry point; Grok Video is included in the main application."""
from main import app

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=30000, log_config=None, log_level="info")
