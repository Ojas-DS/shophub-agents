import os

import uvicorn

from shophub.common.logging import setup_logging
from shophub.config import get_settings
from shophub.orchestrator.api import create_app

if __name__ == "__main__":
    settings = get_settings()
    setup_logging("orchestrator", settings.log_level)
    uvicorn.run(create_app(settings), host="0.0.0.0", port=int(os.getenv("PORT", "8002")), log_config=None)
