"""Run the local Transcribe web app."""

import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "webapp.app:app",
        host="127.0.0.1",
        port=8770,
        timeout_graceful_shutdown=10,
    )
