"""Stage job artifacts on the destination volume before publishing them."""
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


@contextmanager
def staging_directory(parent, *, prefix):
    if os.name != "nt":
        with tempfile.TemporaryDirectory(dir=parent, prefix=prefix) as path:
            yield Path(path)
        return

    # TemporaryDirectory uses mode 0700 on Windows, which replaces inherited ACLs.
    # Same-volume moves preserve those ACLs, so let the staging directory inherit them.
    path = parent / (prefix + uuid4().hex)
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path)
