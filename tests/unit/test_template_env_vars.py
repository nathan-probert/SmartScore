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


class CfnValueLoader(yaml.SafeLoader):
    """SafeLoader that KEEPS intrinsic-tag values so Resource lists/Grant shapes stay inspectable."""


def _keep_intrinsic(loader, suffix, node):
    if isinstance(node, yaml.SequenceNode):
        return [getattr(child, "value", _keep_intrinsic(loader, suffix, child)) for child in node.value]
    return loader.construct_scalar(node)


CfnValueLoader.add_multi_constructor("!", _keep_intrinsic)


def _find_template() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "templates" / "template.yaml"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("could not locate templates/template.yaml")


TEMPLATE_PATH = _find_template()
TEMPLATE_TEXT = TEMPLATE_PATH.read_text(encoding="utf-8")
TEMPLATE = yaml.load(TEMPLATE_TEXT, Loader=CfnLoader)
VALUE_TEMPLATE = yaml.load(TEMPLATE_TEXT, Loader=CfnValueLoader)

# Credentials shared by every handler: the Supabase/PostHog clients, which
# player_archive also needs because Player-Snapshots has RLS on and no policies
# (service role only). Scoped per-handler credentials (Brevo) are deliberately
# excluded - only SendEmailsFunction needs those.
SHARED_ENV_VARS = (
    "ENV",
    "SUPABASE_URL",
    "SUPABASE_API_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "POSTHOG_FEATURE_FLAG_KEY",
)

# Architecture A relay only: MLPicksKick performs a single GitHub
# repository_dispatch (stdlib urllib) and deliberately touches no database,
# so Supabase/PostHog wiring (and its cost of keeping them secret in a
# no-write Lambda) is intentionally absent. Its real dependency is the
# dispatch token, asserted separately in RelayEnvVars below.
RELAY_ONLY_FUNCTIONS = frozenset({"MLPicksKickFunction"})

# Per-function scoped credentials not covered by SHARED_ENV_VARS.
RELAY_ENV_VARS = {"MLPicksKickFunction": ("GITHUB_DISPATCH_TOKEN",)}


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
    """Every handler can reach Supabase and PostHog.

    A new Lambda added to the template without this variable would otherwise
    only fail when that handler first runs. Relay-only functions
    (RELAY_ONLY_FUNCTIONS) are exempt - they hold no DB access by design.
    """
    missing = [
        name
        for name, resource in lambda_functions().items()
        if env_var not in env_vars(resource) and name not in RELAY_ONLY_FUNCTIONS
    ]

    assert not missing, (
        f"{len(missing)} Lambda function(s) missing {env_var}: {sorted(missing)}. "
        f"Add it to the Environment.Variables block of each."
    )


@pytest.mark.parametrize("function_name,env_var", [(f, e) for f, vars_ in RELAY_ENV_VARS.items() for e in vars_])
def test_relay_function_gets_its_scoped_env_var(function_name, env_var):
    """Every relay-only Lambda still gets its own scoped credentials.

    MLPicksKick's GitHub dispatch is a 401-only failure at runtime without
    GITHUB_DISPATCH_TOKEN wired through the template's NoEcho parameter -
    exactly the class of silent breakage this file exists to prevent.
    """
    resource = lambda_functions().get(function_name)
    assert resource is not None, f"{function_name} missing from template.yaml"
    assert env_var in env_vars(resource), f"{function_name} is missing {env_var} (Architecture A per-game relay)"


def test_no_lambda_still_receives_the_retired_worker_token():
    """The smartscore-api worker is gone (#113); its token must not linger.

    A leftover variable is dead weight in every Lambda's environment and a
    standing invitation to re-wire the retired path.
    """
    still_wired = [
        name for name, resource in lambda_functions().items() if "SMARTSCORE_API_TOKEN" in env_vars(resource)
    ]

    assert not still_wired, f"{sorted(still_wired)} still receive SMARTSCORE_API_TOKEN"
    assert "SmartscoreApiToken" not in TEMPLATE.get("Parameters", {}), (
        "the retired worker token parameter is still declared"
    )
    assert "SMARTSCORE_API_TOKEN" not in TEMPLATE_TEXT


def test_every_ref_points_at_a_declared_parameter_or_resource():
    """Catches typos in !Ref targets, which CloudFormation only rejects at deploy."""
    declared = set(TEMPLATE.get("Parameters", {})) | set(TEMPLATE.get("Resources", {}))
    referenced = set(re.findall(r"!Ref\s+([A-Za-z0-9_]+)", TEMPLATE_TEXT))

    undeclared = sorted(referenced - declared)
    assert not undeclared, f"!Ref to undeclared parameter(s)/resource(s): {undeclared}"


def test_kick_ml_picks_role_and_permission_wiring():
    """KickMLPicks wiring (PR #146 follow-up race fix).

    The state machine's KickMLPicks Task invokes MLPicksKick at the END of
    the run, so (a) the StateMachineExecutionRole must grant
    lambda:InvokeFunction on the relay, and (b) the invoke permission must
    name states.amazonaws.com - the state machine is the principal, EventBridge
    is no longer an MLPicksKick invoker.
    """
    role = VALUE_TEMPLATE["Resources"]["StateMachineExecutionRole"]["Properties"]
    policy_document = role["Policies"][0]["PolicyDocument"]
    invoke_statement = next(
        statement for statement in policy_document["Statement"] if "lambda:InvokeFunction" in statement["Action"]
    )
    assert any(resource == "MLPicksKickFunction.Arn" for resource in invoke_statement["Resource"]), (
        "StateMachineExecutionRole missing lambda:InvokeFunction on MLPicksKickFunction - "
        "the KickMLPicks state would fail with AccessDenied"
    )

    permission = VALUE_TEMPLATE["Resources"]["MLPicksKickStatePermission"]["Properties"]
    assert permission["Principal"] == "states.amazonaws.com"
    assert "stateMachine:PlayerProcessingPipeline-${ENV}" in permission["SourceArn"]

    # The old EventBridge-per-rule grant is retired: no events.amazonaws.com
    # principal invokes the relay.
    assert "MLPicksKickFunctionPermission" not in VALUE_TEMPLATE["Resources"]
    for name, resource in VALUE_TEMPLATE["Resources"].items():
        if resource.get("Type") != "AWS::Lambda::Permission":
            continue
        properties = resource.get("Properties", {})
        if "MLPicksKickFunction" in str(properties.get("FunctionName", "")):
            assert properties["Principal"] == "states.amazonaws.com", (
                f"{name} must not re-introduce an EventBridge principal"
            )


def test_kick_ml_picks_asl_state_points_at_the_relay_function():
    """The pipeline ASL's final state invokes the relay by its function name."""
    asl_text = (TEMPLATE_PATH.parent / "player_processing_pipeline.asl.json").read_text(encoding="utf-8")
    assert "function:MLPicksKick-${ENV}" in asl_text, "KickMLPicks state must invoke MLPicksKick-${ENV}"
    assert "MLPicksKickStatePermission" in VALUE_TEMPLATE["Resources"]


def _deploy_script() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "build_scripts" / "deploy.sh"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("could not locate build_scripts/deploy.sh")


def _deployed_function_names() -> set[str]:
    """Function names build_scripts/deploy.sh pushes real code to."""
    text = _deploy_script().read_text(encoding="utf-8")
    block = re.search(r"LAMBDA_FUNCTIONS=\((.*?)\)", text, re.S)
    assert block, "could not locate LAMBDA_FUNCTIONS array in deploy.sh"
    return set(re.findall(r'"([A-Za-z0-9_]+)-\$ENV"', block.group(1)))


def _declared_function_names() -> set[str]:
    """Function names declared as Lambda resources in template.yaml."""
    return set(re.findall(r'FunctionName: !Sub "([A-Za-z0-9_]+)-\$\{ENV\}"', TEMPLATE_TEXT))


def test_every_lambda_gets_its_code_deployed():
    """Each declared Lambda must be in deploy.sh's LAMBDA_FUNCTIONS list.

    CloudFormation only creates the function, with the inline ZipFile
    placeholder. Real code is pushed separately by deploy.sh, so a Lambda
    missing from that list ships as a stub that returns
    {"status": "Lambda function placeholder"} - a deployment that looks
    successful and does nothing.
    """
    declared = _declared_function_names()
    deployed = _deployed_function_names()

    assert declared, "expected to parse FunctionName entries from template.yaml"
    missing = sorted(declared - deployed)
    assert not missing, (
        f"{len(missing)} Lambda function(s) declared but never given real code: {missing}. "
        f"Add each to LAMBDA_FUNCTIONS in build_scripts/deploy.sh."
    )
