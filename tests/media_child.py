"""Child process entry for the integration fixture using deterministic scene authoring."""
import json
import sys
import time
from pathlib import Path

from test_production import author_fixture

from film_worker.production import Production, run_child


def approve_fixture(_, prompt, directory, fingerprint, cancelled, deadline, *, preview):
    index = directory / "frames.json"
    while not cancelled.is_set() and time.monotonic() < deadline:
        evidence = json.loads(index.read_text(encoding="utf-8"))
        if evidence["complete"]:
            return json.dumps(dict(verdict="pass", inspected=evidence["available"], issues=[],
                                   review_token=evidence["review_token"]))
        cancelled.wait(0.05)
    raise RuntimeError("Fixture audit ended before the complete frame list was published")


if __name__=="__main__":
    Production.author = author_fixture
    Production.run_auditor = approve_fixture
    raise SystemExit(run_child(Path(sys.argv[1])))
