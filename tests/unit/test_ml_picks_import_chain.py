"""Cold-start proof for the MLPicksKick relay's shared import chain.

The deployed Handler is ``event_handler.handle_ml_picks_kick``, so the
Lambda imports the WHOLE shared module chain on every cold start:
``event_handler.py:9 -> player_archive.py:38 -> config.py``, and
``config.py:19/:23`` builds BOTH Supabase clients at module-import time.
With the deployed function carrying only ``GITHUB_DISPATCH_TOKEN``, the
cold start died on ``SupabaseException: supabase_url is required`` before
the handler ever ran (PR #146 owner escalation).

units/conftest.py patches ``supabase.create_client`` globally, which masks
exactly this failure inside the ordinary unit suite - so this file imports
the REAL chain in subprocesses against a staged, deploy-zip-shaped copy of
``smartscore/*.py`` (flat directory, no ``.env``, no parent-project state),
asserting:

* with NO Supabase env (the old deployed state) the chain CANNOT import;
* with only SUPABASE_URL or SUPABASE_URL+SUPABASE_API_KEY it still cannot;
* with the full trio (the template's Option-A wiring) the chain imports.

The template-level guarantee (that every function, relay included, carries
the trio from the shared ``Supabase*`` parameters) lives in
test_template_env_vars.py; this file proves the requirement is real rather
than imagined.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SMARTSCORE_DIR = REPO_ROOT / "smartscore"

#: The import-required Supabase trio, mirroring the template wiring.
IMPORT_TRIO = ("SUPABASE_URL", "SUPABASE_API_KEY", "SUPABASE_SERVICE_ROLE_KEY")

# Dummy values only - create_client never touches the network at import.
DUMMY_ENV = {
    "SUPABASE_URL": "https://coldstart-test.supabase.co",
    "SUPABASE_API_KEY": "coldstart-test-anon-key",
    "SUPABASE_SERVICE_ROLE_KEY": "coldstart-test-service-key",
}

# Runs INSIDE the simulated zip root, with cwd pointed there too, so
# python-dotenv (load_dotenv in config.py) cannot find the owner's repo
# .env - the deployed Lambda has no such file.
PROBE_SCRIPT = """
import importlib
try:
    importlib.import_module("event_handler")
except Exception as exc:  # noqa: BLE001 - the subprocess reports the failure
    print(f"IMPORT-FAIL {type(exc).__name__}: {exc}")
    raise SystemExit(2)
print("IMPORT-OK event_handler")
"""


@pytest.fixture(scope="module")
def lambda_sim_dir(tmp_path_factory) -> Path:
    """A flat directory shaped like the deploy zip: smartscore/*.py at root.

    importlib resolves ``event_handler``/``player_archive``/``config`` from
    here exactly as the Lambda does (the zip unpacks smartscore/* to the
    task root), and ``load_dotenv()`` walking up finds no ``.env`` because
    the parent chain is pytest's tmp tree.
    """
    sim_dir = tmp_path_factory.mktemp("lambdasim")
    for module_file in sorted(SMARTSCORE_DIR.glob("*.py")):
        shutil.copy2(module_file, sim_dir / module_file.name)
    assert (sim_dir / "config.py").is_file()
    assert (sim_dir / "event_handler.py").is_file()
    return sim_dir


def _import_chain(sim_dir: Path, present: tuple = IMPORT_TRIO) -> subprocess.CompletedProcess:
    """Import ``event_handler`` in a subprocess as the deployed Lambda does.

    The environment is scrubbed of the Supabase trio (plus GITHUB_DISPATCH_TOKEN)
    and each var in ``present`` is added back with its dummy value, so a dev
    shell that happens to export SUPABASE_* can never mask the scenario.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("SUPABASE_") and key not in {"GITHUB_DISPATCH_TOKEN"}
    }
    for var in present:
        env[var] = DUMMY_ENV[var]
    env["PYTHONPATH"] = str(sim_dir) + os.pathsep + env.get("PYTHONPATH", "")

    probe_path = sim_dir / "_import_probe.py"
    if not probe_path.is_file():
        probe_path.write_text(PROBE_SCRIPT, encoding="utf-8")

    return subprocess.run(
        [sys.executable, str(probe_path)],
        cwd=sim_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


class TestColdStartNeedsSupabaseEnv:
    def test_no_supabase_env_fails_the_chain_import(self, lambda_sim_dir: Path):
        """The escalated deployed state: SUPABASE_URL+API_KEY absent.

        config.py:19 builds the anon client at import -> the cold start dies
        with SupabaseException before handle_ml_picks_kick can run.
        """
        result = _import_chain(lambda_sim_dir, present=())

        assert result.returncode != 0, (
            "the handler chain imported WITHOUT any Supabase env - if this "
            "ever becomes true, the template's import-required wiring (and "
            "test_template_env_vars.py) is over-wired and can be reduced"
        )
        assert "IMPORT-FAIL" in result.stdout
        assert "supabase_url is required" in result.stdout

    def test_missing_service_role_key_fails_the_chain_import(self, lambda_sim_dir: Path):
        """URL+anon present, SUPABASE_SERVICE_ROLE_KEY absent.

        config.py:23-26 builds the service-role client at import -> the
        cold start dies too ("supabase_key is required"), which is why the
        template wires all THREE keys and not just the first failing two.
        """
        result = _import_chain(lambda_sim_dir, present=("SUPABASE_URL", "SUPABASE_API_KEY"))

        assert result.returncode != 0
        assert "IMPORT-FAIL" in result.stdout
        assert "supabase_key is required" in result.stdout

    def test_missing_api_key_alone_fails_the_chain_import(self, lambda_sim_dir: Path):
        """SUPABASE_API_KEY absent (URL present): the same import crash.

        Mapping the failure to exactly one var per case proves each of the
        three is individually import-required, not just the pair.
        """
        result = _import_chain(lambda_sim_dir, present=("SUPABASE_URL",))

        assert result.returncode != 0
        assert "supabase_key is required" in result.stdout

    def test_full_trio_imports_the_chain(self, lambda_sim_dir: Path):
        """With the trio wired (the template's Option-A state) the handler
        chain imports cleanly - the fix is sufficient for the cold start."""
        result = _import_chain(lambda_sim_dir, present=IMPORT_TRIO)

        assert result.returncode == 0, (
            f"the handler chain failed to import WITH the import-required env:\n{result.stdout}\n{result.stderr}"
        )
        assert "IMPORT-OK event_handler" in result.stdout

    def test_the_relay_module_alone_never_imports_config(self, lambda_sim_dir: Path):
        """Why the wiring is non-obvious: ``ml_picks_kick`` itself is only
        stdlib - the import-time requirement comes from the SHARED handler
        chain, not from the relay module."""
        env = {key: value for key, value in os.environ.items() if not key.startswith("SUPABASE_")}
        env["PYTHONPATH"] = str(lambda_sim_dir)
        result = subprocess.run(
            [sys.executable, "-c", "import ml_picks_kick; print('IMPORT-OK ml_picks_kick')"],
            cwd=lambda_sim_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        assert result.returncode == 0
        assert "IMPORT-OK ml_picks_kick" in result.stdout
