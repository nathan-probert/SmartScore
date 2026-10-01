"""Guards on the Lambda environment wiring in templates/template.yaml.

A Lambda missing an env var it needs fails only at runtime, as a 401 or a
KeyError, long after CI goes green. ``dev-integration`` does not exercise
every handler, so these assertions are the only thing standing between a
forgotten variable and production. Issue #117 lost a day to exactly that:
SMARTSCORE_API_TOKEN was added to SavePlayersFunction but not to
PerformBackfillingFunction or UpdateHistoryFunction, which call the same
worker.
"""

import re
from pathlib import Path

import pytest
import yaml


class CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation intrinsic tags."""


CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: {"__cfn_tag__": suffix})


def _find_template() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "templates" / "template.yaml"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("could not locate templates/template.yaml")


TEMPLATE_PATH = _find_template()
TEMPLATE_TEXT = TEMPLATE_PATH.read_text(encoding="utf-8")
TEMPLATE = yaml.load(TEMPLATE_TEXT, Loader=CfnLoader)

# Credentials shared by every handler: the Supabase/PostHog clients and the
# Cloudflare smartscore-api worker. Scoped per-handler credentials (Brevo) are
# deliberately excluded - only SendEmailsFunction needs those.
SHARED_ENV_VARS = (
    "ENV",
    "SUPABASE_URL",
    "SUPABASE_API_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "POSTHOG_FEATURE_FLAG_KEY",
    "SMARTSCORE_API_TOKEN",
)


def lambda_functions() -> dict:
    return {
        name: resource
        for name, resource in TEMPLATE["Resources"].items()
        if resource.get("Type") == "AWS::Lambda::Function"
    }


def env_vars(resource: dict) -> dict:
    return resource.get("Properties", {}).get("Environment", {}).get("Variables", {})


def test_template_parses_and_has_lambdas():
    functions = lambda_functions()
    assert functions, "expected at least one AWS::Lambda::Function in the template"


@pytest.mark.parametrize("env_var", SHARED_ENV_VARS)
def test_every_lambda_function_gets_shared_env_var(env_var):
    """Every handler can reach Supabase, PostHog and the worker.

    A new Lambda added to the template without this variable would otherwise
    only fail when that handler first runs.
    """
    missing = [name for name, resource in lambda_functions().items() if env_var not in env_vars(resource)]

    assert not missing, (
        f"{len(missing)} Lambda function(s) missing {env_var}: {sorted(missing)}. "
        f"Add it to the Environment.Variables block of each."
    )


def test_every_ref_points_at_a_declared_parameter():
    """Catches typos in !Ref targets, which CloudFormation only rejects at deploy."""
    declared = set(TEMPLATE.get("Parameters", {}))
    referenced = set(re.findall(r"!Ref\s+([A-Za-z0-9_]+)", TEMPLATE_TEXT))

    undeclared = sorted(referenced - declared)
    assert not undeclared, f"!Ref to undeclared parameter(s): {undeclared}"


def test_worker_token_parameter_is_noecho():
    """The worker token is a bearer credential; keep it out of console output."""
    assert TEMPLATE["Parameters"]["SmartscoreApiToken"].get("NoEcho") is True
