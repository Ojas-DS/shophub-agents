"""`python -m shophub.store_api` - run the mock store on :8000."""

import os

import uvicorn

from shophub.common.logging import setup_logging
from shophub.config import get_settings
from shophub.store_api.app import create_app

if __name__ == "__main__":
    setup_logging("store_api", get_settings().log_level)
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8000")), log_config=None)
