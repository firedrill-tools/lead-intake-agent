"""Exercise bundled data/initialization outside the checkout without credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile

wheel, = Path("dist").glob("*.whl")
environment = {key: value for key, value in os.environ.items() if not key.startswith(("GEMINI_", "GOOGLE_", "INTAKE_", "PIPEDREAM_"))}
environment.pop("ALLOW_PIPEDREAM_ACTIONS", None)
with ZipFile(wheel) as archive:
    assert "lead_intake/sample_data/manifest.json" in archive.namelist()
    assert len([name for name in archive.namelist() if name.startswith("lead_intake/sample_data/") and name.endswith(".json")]) == 23
    with TemporaryDirectory(prefix="lead-intake-wheel-") as directory:
        archive.extractall(directory)
        script = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from lead_intake import cli
assert Path(cli.__file__).is_relative_to(Path(sys.argv[1]))
assert cli.main(["--list-cases"]) == 0
raise SystemExit(cli.main(["--init-case", "busy-existing", "--db", str(Path(sys.argv[1]) / "state.sqlite"), "--request-out", str(Path(sys.argv[1]) / "request.json")]))
'''
        checked = subprocess.run([sys.executable, "-c", script, directory], cwd=directory, env=environment,
                                 text=True, capture_output=True, check=True)
        listed, initialized = [json.loads(line) for line in checked.stdout.splitlines()]
        assert listed["case_count"] == 10 and listed["version"] == "1.0.0"
        assert initialized["contacts"] == 12 and initialized["prior_events"] == 2
        assert initialized["model_called"] is False
print("Packaged CLI validates ten cases and initializes busy SQLite state outside the checkout; no model called.")
