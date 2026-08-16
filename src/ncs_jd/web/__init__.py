"""Local FastAPI entry point for the NCS JD review workspace."""

from ncs_jd.web.app import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    create_app,
    create_connected_app,
)

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "create_app",
    "create_connected_app",
]
