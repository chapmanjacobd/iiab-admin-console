"""Load the command-server scripts as importable modules for the tests.

`iiab-cmdsrv3.py` and `iiab-syncweb-fetch.py` are deployed scripts, not
packages, and the command server imports services that are only present on an
IIAB.  Load them by path and stub the runtime-only dependencies so the unit
tests can run on a development machine.
"""

import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / 'roles' / 'cmdsrv' / 'files' / 'scripts'


class _JournalHandler(logging.Handler):
    def __init__(self, *args, **kwargs):
        super().__init__()

    def emit(self, record):
        pass


def _stub(name, **attributes):
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='session')
def syncweb_fetch():
    return _load('iiab_syncweb_fetch', SCRIPTS_DIR / 'iiab-syncweb-fetch.py')


@pytest.fixture(scope='session')
def cmdsrv():
    _stub('systemd', journal=_stub('systemd.journal', JournalHandler=_JournalHandler))
    _stub('zmq')
    _stub('cracklib')
    iiab = _stub('iiab')
    iiab.adm_lib = _stub('iiab.adm_lib')
    return _load('iiab_cmdsrv3', REPO_ROOT / 'roles' / 'cmdsrv' / 'files' / 'iiab-cmdsrv3.py')
