from pathlib import Path

import pytest

from deepqueue.config import initialize
from deepqueue.db import Database
from deepqueue.models import Server


@pytest.fixture
def db(tmp_path: Path):
    home = tmp_path / "queue"
    initialize(home)
    database = Database(home)
    database.initialize()
    database.add_server(Server(name="local", worker_root=str(home / "worker")))
    return database
