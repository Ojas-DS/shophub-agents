"""`python -m shophub.orders_agent` - run the Orders Agent (A2A server) on :8001."""

import os

import uvicorn

from shophub.common.logging import setup_logging
from shophub.config import get_settings
from shophub.orders_agent.server import create_app

if __name__ == "__main__":
    settings = get_settings()
    setup_logging("orders_agent", settings.log_level)
    uvicorn.run(create_app(settings), host="0.0.0.0", port=int(os.getenv("PORT", "8001")), log_config=None)
