"""Keep tests away from the real Orca: no runtime metadata to find, and no shared rate-limit or reminder state.

Imported first by every test module that drives route.py. Child processes inherit the environment.
"""
import atexit
import json
import os
import shutil
import tempfile
from pathlib import Path

_root = tempfile.mkdtemp(prefix='orca-routing-test-')
atexit.register(shutil.rmtree, _root, ignore_errors=True)
os.environ['ORCA_ROUTING_STATE_DIR'] = os.path.join(_root, 'state')
os.environ['ORCA_USER_DATA_PATH'] = os.path.join(_root, 'no-orca')

CONFIG_PATH = Path(__file__).resolve().parent / 'fixtures' / 'routes.test.json'


def read_routes():
    return json.loads(CONFIG_PATH.read_text())
