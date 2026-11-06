"""WSGI entry point for Apache mod_wsgi (see apache/video-service.conf)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from web_service import app as application  # noqa: E402,F401
