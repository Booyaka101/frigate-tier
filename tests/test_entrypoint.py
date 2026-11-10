"""Exercise the POSIX container runner with a recording CLI stub."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX container runner")
ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker-entrypoint.sh"


@pytest.fixture
def run_entrypoint(tmp_path):
    executable = tmp_path / "frigate-tier"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CAPTURE'], 'a') as out:\n"
        "    out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "code = int(os.environ.get('FAIL_MOVE', '0'))\n"
        "sys.exit(code if sys.argv[1] == 'move' else 0)\n"
    )
    executable.chmod(0o755)
    capture = tmp_path / "calls.jsonl"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("FRIGATE_TIER_")
    }
    env.update(
        PATH=str(tmp_path) + os.pathsep + env["PATH"],
        CAPTURE=str(capture),
        FRIGATE_TIER_COLD="/cold archive",
        FRIGATE_TIER_INTERVAL="0",
    )

    def run(args=(), **settings):
        result = subprocess.run(
            ["sh", str(ENTRYPOINT), *args],
            env={**env, **settings},
            capture_output=True,
            text=True,
            timeout=15,
        )
        calls = (
            [json.loads(line) for line in capture.read_text().splitlines()]
            if capture.exists()
            else []
        )
        return result, calls

    return run


def test_runner_preserves_quoted_roots_and_does_not_expand_globs(run_entrypoint):
    result, calls = run_entrypoint(
        FRIGATE_TIER_PREVIEW_HOT="/hot previews",
        FRIGATE_TIER_PREVIEW_COLD="/cold previews",
        FRIGATE_TIER_VERIFY="1",
        FRIGATE_TIER_SYNC_REPORT="1",
        FRIGATE_TIER_DB_PATH_PREFIX="/archive=/media/archive /previews=/media/previews",
        FRIGATE_TIER_UNTIL_FREE="20%",
        FRIGATE_TIER_MIN_FREE_ON_COLD="1G",
        FRIGATE_TIER_BANDWIDTH_LIMIT="50M",
        FRIGATE_TIER_ARGS="--camera *",
    )
    assert result.returncode == 0, result.stderr
    assert [call[0] for call in calls] == ["move", "verify", "verify", "sync-report"]
    move = calls[0]
    for flag, expected in [
        ("--cold", "/cold archive"),
        ("--preview-hot", "/hot previews"),
        ("--preview-cold", "/cold previews"),
        ("--until-free", "20%"),
        ("--min-free-on-cold", "1G"),
        ("--bandwidth-limit", "50M"),
        ("--camera", "*"),
    ]:
        assert move[move.index(flag) + 1] == expected
    assert "--commit" in move
    for call in calls:
        assert call.count("--db-path-prefix") == 2
        assert "/archive=/media/archive" in call
        assert "/previews=/media/previews" in call
    assert calls[2][calls[2].index("--cold") + 1] == "/cold previews"
    assert calls[3][calls[3].index("--previews-root") + 1] == "/hot previews"


@pytest.mark.parametrize(
    "root", ["FRIGATE_TIER_PREVIEW_HOT", "FRIGATE_TIER_PREVIEW_COLD"]
)
def test_runner_rejects_an_incomplete_preview_pair(run_entrypoint, root):
    result, calls = run_entrypoint(**{root: "/previews"})
    assert result.returncode == 1
    assert "Set both" in result.stderr
    assert calls == []


def test_runner_dry_run_omits_commit(run_entrypoint):
    result, calls = run_entrypoint(FRIGATE_TIER_DRY_RUN="1", FRIGATE_TIER_I_KNOW="1")
    assert result.returncode == 0
    assert "--commit" not in calls[0]
    assert "--i-know" in calls[0]


def test_runner_preserves_failure_even_when_followup_checks_pass(run_entrypoint):
    result, calls = run_entrypoint(FAIL_MOVE="3", FRIGATE_TIER_VERIFY="1")
    assert result.returncode == 3
    assert [call[0] for call in calls] == ["move", "verify"]


def test_explicit_arguments_bypass_the_loop(run_entrypoint):
    result, calls = run_entrypoint(["--help"], FRIGATE_TIER_COLD="")
    assert result.returncode == 0
    assert calls == [["--help"]]
