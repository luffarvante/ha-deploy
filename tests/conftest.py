"""Make the app module importable for tests (app lives outside tests/)."""
import os
import sys

APP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "ha_deploy", "rootfs", "app")
)
sys.path.insert(0, APP_DIR)
