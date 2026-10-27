"""
Golden-output test: the performance refactors must not change what the dashboard or the
database sees. tests/golden/baseline.json was captured from the code BEFORE any optimisation.

    venv/bin/python -m unittest tests.test_golden -v
    venv/bin/python tests/test_golden.py --update     # only when a change is intended
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden" / "baseline.json"


def capture() -> dict:
    env = dict(os.environ, PYTHONHASHSEED="0")
    out = subprocess.run([sys.executable, str(HERE / "golden_capture.py")],
                         env=env, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def diff(a, b, path="", out=None, limit=15):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}/{k}: only in {'golden' if k in a else 'new'}")
            else:
                diff(a[k], b[k], f"{path}/{k}", out, limit)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} vs {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{path}[{i}]", out, limit)
    elif a != b:
        out.append(f"{path}: golden={a!r} new={b!r}")
    return out


class GoldenTest(unittest.TestCase):
    def test_outputs_unchanged(self):
        golden = json.loads(GOLDEN.read_text())
        new = capture()
        problems = diff(golden, new)
        self.assertEqual(problems, [], "\n" + "\n".join(problems))


if __name__ == "__main__":
    if "--update" in sys.argv:
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(capture(), sort_keys=True))
        print(f"wrote {GOLDEN}")
    else:
        unittest.main()
