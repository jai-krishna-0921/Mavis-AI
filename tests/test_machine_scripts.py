"""The AWS scripts are idempotent and dry-run by default. A stub `aws` on PATH records every call."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MUTATING = ("create-", "put-", "attach-", "associate-", "modify-", "add-role", "delete-")

STUB = r"""#!/usr/bin/env python3
import json, os, sys
args = [a for a in sys.argv[1:]]
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
exists = os.environ.get("STUB_EXISTS") == "1"
joined = " ".join(args)
def out(s): print(s); sys.exit(0)
if "sts get-caller-identity" in joined: out("276307603629")
if "describe-instances" in joined:
    if "HttpPutResponseHopLimit" in joined: out("2" if exists else "1")
    if "HttpTokens" in joined: out("required" if exists else "optional")
    out("")
if "describe-iam-instance-profile-associations" in joined:
    out("arn:aws:iam::276307603629:instance-profile/mavis-ec2" if exists else "")
if "get-role " in joined and os.environ.get("STUB_ROLE") == "1": out("{}")
if any(v in joined for v in ("get-role ", "get-instance-profile", "head-bucket", "describe-budget",
                              "list-code-interpreters")):
    if not exists: sys.exit(254)
    if "Roles[].RoleName" in joined: out("mavis-ec2")
    if "list-code-interpreters" in joined: out("mavis_ci_sandbox-abc123")
    out("{}")
if "list-service-quotas" in joined: out("[]")
sys.exit(0)
"""


@pytest.fixture
def stub(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    aws = bindir / "aws"
    aws.write_text(STUB)
    aws.chmod(aws.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state.env"
    state.write_text("MAVIS_INSTANCE_ID=i-0e39253adaacfd498\nMAVIS_EIP=203.0.113.9\n")
    log = tmp_path / "aws.log"

    def run(
        script: str, *args: str, exists: bool = False, email: str = "", role: bool = True
    ) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
        env = {
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "STUB_LOG": str(log),
            "STUB_EXISTS": "1" if exists else "0",
            "MAVIS_STATE_FILE": str(state),
            "MAVIS_BUDGET_EMAIL": email,
            "STUB_ROLE": "1" if role and script == "machine.sh" else "0",
            "MAVIS_SKIP_PROPAGATION_WAIT": "1",
        }
        if log.exists():
            log.unlink()
        proc = subprocess.run(
            ["bash", str(ROOT / "deploy" / "aws" / script), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return proc, calls

    return run


def _mutations(calls):
    return [
        c
        for c in calls
        if any(c[i].startswith(MUTATING) for i in range(len(c)) if i < 6 and not c[i].startswith("-"))
    ]


@pytest.mark.parametrize("script", ["iam-role.sh", "machine.sh"])
def test_syntax(script):
    subprocess.run(["bash", "-n", str(ROOT / "deploy" / "aws" / script)], check=True)


@pytest.mark.parametrize("script", ["iam-role.sh", "machine.sh"])
def test_dry_run_is_the_default_and_mutates_nothing(stub, script):
    proc, calls = stub(script)
    assert proc.returncode == 0, proc.stderr
    assert _mutations(calls) == []
    assert "PLAN: aws " in proc.stdout


def test_iam_role_apply_creates_role_profile_and_imds(stub):
    proc, calls = stub("iam-role.sh", "--apply")
    assert proc.returncode == 0, proc.stderr
    verbs = [" ".join(c[c.index("iam") if "iam" in c else c.index("ec2") :][:2]) for c in _mutations(calls)]
    assert verbs == [
        "iam create-role",
        "iam create-instance-profile",
        "iam add-role-to-instance-profile",
        "ec2 associate-iam-instance-profile",
        "ec2 modify-instance-metadata-options",
    ]
    imds = next(c for c in calls if "modify-instance-metadata-options" in c)
    assert imds[imds.index("--http-put-response-hop-limit") + 1] == "2"
    assert imds[imds.index("--http-tokens") + 1] == "required"
    assert not any(
        "put-role-policy" in c or "attach-role-policy" in c for c in calls
    )  # contract B: no policies


def test_iam_role_apply_twice_creates_nothing_new(stub):
    proc, calls = stub("iam-role.sh", "--apply", exists=True)
    assert proc.returncode == 0
    # only the idempotent metadata-options call may repeat
    assert [c for c in _mutations(calls) if "modify-instance-metadata-options" not in c] == []


def test_machine_apply_needs_the_budget_email(stub):
    proc, _ = stub("machine.sh", "--apply")
    assert proc.returncode != 0 and "MAVIS_BUDGET_EMAIL" in proc.stderr


def test_machine_apply_creates_exact_resources(stub):
    proc, calls = stub("machine.sh", "--apply", email="owner@example.com")
    assert proc.returncode == 0, proc.stderr
    flat = [" ".join(c) for c in calls]
    assert any("create-bucket" in f and "mavis-machine-276307603629-aps1" in f for f in flat)
    assert any("put-public-access-block" in f and "BlockPublicAcls=true" in f for f in flat)
    assert any("put-bucket-encryption" in f and "AES256" in f for f in flat)
    assert any("put-bucket-ownership-controls" in f and "BucketOwnerEnforced" in f for f in flat)
    lifecycle = json.loads(next(c for c in calls if "put-bucket-lifecycle-configuration" in c)[-1])
    rules = {r["ID"]: r for r in lifecycle["Rules"]}
    assert set(rules) == {"work-14d", "inbox-60d", "out-60d", "tmp-1d", "e2e-30d", "abort-mpu-1d"}
    assert rules["work-14d"]["Filter"]["Tag"] == {"Key": "cls", "Value": "work"}
    policy_call = next(c for c in calls if "put-role-policy" in c)
    assert policy_call[policy_call.index("--policy-name") + 1] == "mavis-machine"
    policy = json.loads(policy_call[policy_call.index("--policy-document") + 1])
    actions = [
        a
        for st in policy["Statement"]
        for a in ([st["Action"]] if isinstance(st["Action"], str) else st["Action"])
    ]
    assert not any(a.startswith(("iam:", "ec2:", "bedrock-agentcore-control:")) for a in actions)
    assert "bedrock-agentcore:StartCodeInterpreterSession" in actions and "s3:DeleteObject" in actions
    resources = json.dumps(policy)
    assert "arn:aws:s3:::mavis-machine-276307603629-aps1/*" in resources
    budget = next(c for c in calls if "create-budget" in c)
    body = json.loads(budget[budget.index("--budget") + 1])
    assert body["BudgetName"] == "mavis-machine-monthly" and body["BudgetLimit"]["Amount"] == "20"
    assert not any("create-code-interpreter" in f for f in flat)  # R4b only on request


def test_machine_apply_twice_only_reputs_configuration(stub):
    proc, calls = stub("machine.sh", "--apply", exists=True, email="owner@example.com")
    assert proc.returncode == 0
    assert not any(" ".join(c).find("create-") >= 0 for c in _mutations(calls))


def test_custom_interpreter_is_opt_in(stub):
    proc, calls = stub("machine.sh", "--apply", "--custom-interpreter", email="owner@example.com")
    assert proc.returncode == 0
    ci = next(c for c in calls if "create-code-interpreter" in c)
    assert "mavis_ci_sandbox" in ci and "SANDBOX" in " ".join(ci)


def test_machine_apply_never_creates_the_role(stub):
    proc, calls = stub("machine.sh", "--apply", email="owner@example.com", role=False)
    assert proc.returncode != 0 and "iam-role.sh" in proc.stderr
    assert not any("create-role" in c or "put-role-policy" in c for c in calls)
