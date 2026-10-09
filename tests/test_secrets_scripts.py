"""SSM-backed secrets: stubbed aws/ssh/scp/rsync; no value may ever reach stdout, stderr or a logged argv."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AWS_DIR = ROOT / "deploy" / "aws"

AWS_STUB = r"""#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
store_path = os.environ["STUB_SSM"]
store = json.load(open(store_path)) if os.path.exists(store_path) else {}
j = " ".join(a)
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(json.dumps(a) + "\n")
if "sts get-caller-identity" in j:
    print("276307603629"); sys.exit(0)
if "get-parameters-by-path" in j:
    assert "--with-decryption" in a
    params = [{"Name": "/mavis/prod/" + k, "Value": v, "Type": "SecureString"} for k, v in store.items()]
    print(json.dumps({"Parameters": params})); sys.exit(0)
if "put-parameter" in j:
    path = a[a.index("--cli-input-json") + 1]
    assert path.startswith("file://")
    f = path[len("file://"):]
    assert oct(os.stat(f).st_mode & 0o777) == "0o600", "put input must be mode 600"
    body = json.load(open(f))
    assert body["Type"] == "SecureString" and body["KeyId"] == "alias/aws/ssm"
    k = body["Name"].rsplit("/", 1)[-1]
    if k in store: assert body.get("Overwrite") is True
    else: assert body["Tags"] == [{"Key": "Project", "Value": "mavis"}]
    store[k] = body["Value"]
    json.dump(store, open(store_path, "w"))
    with open(os.environ["STUB_PUTS"], "a") as fh: fh.write(k + "\n")
    print(json.dumps({"Version": 1, "Tier": "Standard"})); sys.exit(0)
if "describe-parameters" in j:
    print("\n".join("/mavis/prod/%s\t1.0" % k for k in store)); sys.exit(0)
sys.exit(0)
"""

SSH_STUB = r"""#!/usr/bin/env python3
import os, sys
cmd = " ".join(sys.argv[1:])
with open(os.environ["STUB_SSH_LOG"], "a") as fh:
    fh.write(cmd + "\n")
box = os.environ.get("STUB_BOX_ENV", "")
if "test -f" in cmd and cmd.endswith("/.env"):
    sys.exit(0 if box else 1)
if "cat /opt/mavis/.env" in cmd:
    sys.stdout.write(open(box).read()); sys.exit(0)
if "grep -oE" in cmd:
    names = sorted({l.split("=", 1)[0] for l in open(box) if "=" in l and l.split("=", 1)[1].strip()})
    print("\n".join(names)); sys.exit(0)
if "deploy.rc" in cmd and "cat" in cmd:
    print("0"); sys.exit(0)
sys.exit(0)
"""

OK_STUB = "#!/bin/sh\nexit 0\n"

SECRETS = {
    "POSTGRES_PASSWORD": "pgpw-VALUE-1111",
    "NEO4J_PASSWORD": "neopw-VALUE-2222",
    "TELEGRAM_WEBHOOK_SECRET": "whsec-VALUE-3333",
    "NATIVE_TOKEN_KEK": "kek-VALUE-4444=",
}


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("aws", AWS_STUB), ("ssh", SSH_STUB), ("scp", OK_STUB), ("rsync", OK_STUB)):
        f = bindir / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state.env"
    state.write_text("MAVIS_INSTANCE_ID=i-0e39253adaacfd498\nMAVIS_EIP=203.0.113.9\n")
    ssm = tmp_path / "ssm.json"
    base = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "STUB_SSM": str(ssm),
        "STUB_LOG": str(tmp_path / "aws.log"),
        "STUB_PUTS": str(tmp_path / "puts.log"),
        "STUB_SSH_LOG": str(tmp_path / "ssh.log"),
        "MAVIS_STATE_FILE": str(state),
        "MAVIS_POLL_S": "0",
    }

    class H:
        path = tmp_path

        def store(self, data=None):
            if data is not None:
                ssm.write_text(json.dumps(data))
            return json.loads(ssm.read_text()) if ssm.exists() else {}

        def puts(self):
            f = tmp_path / "puts.log"
            return f.read_text().split() if f.exists() else []

        def calls(self):
            f = tmp_path / "aws.log"
            return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

        def ssh(self):
            f = tmp_path / "ssh.log"
            return f.read_text() if f.exists() else ""

        def reset(self):
            for n in ("puts.log", "aws.log", "ssh.log"):
                (tmp_path / n).unlink(missing_ok=True)

        def run(self, script, *args, stdin="", **extra):
            return subprocess.run(
                ["bash", str(AWS_DIR / script), *args], env={**base, **extra}, input=stdin,
                capture_output=True, text=True, timeout=120,
            )

    return H()


def write_env(path: Path, **kv):
    path.write_text("".join(f"{k}={v}\n" for k, v in kv.items()))
    return path


def no_values(proc, values):
    out = proc.stdout + proc.stderr
    for v in values:
        assert v not in out, "a secret value was printed"


OWNER = {
    "OLLAMA_API_KEY": "ollama-VALUE-aaaa",
    "TAVILY_API_KEY": "tavily-VALUE-bbbb",
    "COMPOSIO_API_KEY": "composio-VALUE-cccc",
    "TELEGRAM_BOT_TOKEN": "123:tg-VALUE-dddd",
    "ALLOWED_TELEGRAM_CHAT_IDS": "111",
}


def test_push_is_a_dry_run_by_default(env):
    f = write_env(env.path / "local.env", **OWNER)
    proc = env.run("secrets.sh", "push", "--file", str(f))
    assert proc.returncode == 0, proc.stderr
    assert env.puts() == [] and "PLAN OLLAMA_API_KEY: new" in proc.stdout
    no_values(proc, OWNER.values())


def test_push_writes_only_changed_keys_and_never_prints_values(env):
    f = write_env(env.path / "local.env", **OWNER, POSTGRES_PASSWORD="dev-VALUE-pg", ENV="dev")
    env.store({"OLLAMA_API_KEY": OWNER["OLLAMA_API_KEY"], "TAVILY_API_KEY": "old-VALUE-tav"})
    proc = env.run("secrets.sh", "push", "--apply", "--file", str(f))
    assert proc.returncode == 0, proc.stderr
    assert sorted(env.puts()) == ["ALLOWED_TELEGRAM_CHAT_IDS", "COMPOSIO_API_KEY", "TAVILY_API_KEY",
                                  "TELEGRAM_BOT_TOKEN"]
    assert "OLLAMA_API_KEY: unchanged" in proc.stdout and "TAVILY_API_KEY: updated" in proc.stdout
    assert "COMPOSIO_API_KEY: new" in proc.stdout
    # a local dev env file never pushes generated secrets or deploy-decided config
    assert "POSTGRES_PASSWORD" not in env.store() and "ENV" not in env.store()
    no_values(proc, [*OWNER.values(), "old-VALUE-tav", "dev-VALUE-pg"])
    # values never appear in any aws argv
    assert all(v not in json.dumps(env.calls()) for v in OWNER.values())
    # second run writes nothing
    env.reset()
    proc = env.run("secrets.sh", "push", "--apply", "--file", str(f))
    assert env.puts() == [] and "new" not in proc.stdout and "updated" not in proc.stdout


def test_push_with_deploy_never_overwrites_generated_secrets(env):
    env.store(dict(SECRETS))
    f = write_env(env.path / "d.env", **OWNER, **{k: "other-" + v for k, v in SECRETS.items()}, ENV="prod")
    proc = env.run("secrets.sh", "push", "--apply", "--with-deploy", "--file", str(f))
    assert proc.returncode == 0, proc.stderr
    assert env.store()["NATIVE_TOKEN_KEK"] == SECRETS["NATIVE_TOKEN_KEK"]
    assert not set(SECRETS) & set(env.puts())
    assert "ENV" in env.puts()
    no_values(proc, [*SECRETS.values(), *["other-" + v for v in SECRETS.values()]])


def test_seed_from_box_creates_missing_and_never_overwrites(env):
    box = write_env(
        env.path / "box.env", **SECRETS, OLLAMA_API_KEY="box-VALUE-oll", ADMIN_PASSWORD="adm-VALUE",
        EMPTYKEY="",
    )
    env.store({"NATIVE_TOKEN_KEK": "ssm-VALUE-kek", "OLLAMA_API_KEY": "ssm-VALUE-oll"})
    proc = env.run("secrets.sh", "seed-from-box", "--apply", STUB_BOX_ENV=str(box))
    assert proc.returncode == 0, proc.stderr
    st = env.store()
    assert st["NATIVE_TOKEN_KEK"] == "ssm-VALUE-kek" and st["OLLAMA_API_KEY"] == "ssm-VALUE-oll"
    assert st["POSTGRES_PASSWORD"] == SECRETS["POSTGRES_PASSWORD"] and st["ADMIN_PASSWORD"] == "adm-VALUE"
    assert "EMPTYKEY" not in st and "NATIVE_TOKEN_KEK" not in env.puts()
    assert "sudo cat /opt/mavis/.env" in env.ssh()
    no_values(proc, [*SECRETS.values(), "box-VALUE-oll", "ssm-VALUE-kek", "adm-VALUE"])


def test_seed_from_box_dry_run_writes_nothing(env):
    box = write_env(env.path / "box.env", **SECRETS)
    proc = env.run("secrets.sh", "seed-from-box", STUB_BOX_ENV=str(box))
    assert proc.returncode == 0 and env.puts() == [] and "PLAN NATIVE_TOKEN_KEK: new" in proc.stdout


def test_list_shows_names_only(env):
    env.store(dict(SECRETS))
    proc = env.run("secrets.sh", "list")
    assert "/mavis/prod/NATIVE_TOKEN_KEK" in proc.stdout
    no_values(proc, SECRETS.values())


def test_set_reads_stdin_and_guards_generated_keys(env):
    env.store(dict(SECRETS))
    proc = env.run("secrets.sh", "set", "NATIVE_TOKEN_KEK", "--apply", stdin="x\n")
    assert proc.returncode != 0 and env.store()["NATIVE_TOKEN_KEK"] == SECRETS["NATIVE_TOKEN_KEK"]
    proc = env.run("secrets.sh", "set", "TELEGRAM_MODE", "--apply", stdin="polling\n")
    assert proc.returncode == 0 and env.store()["TELEGRAM_MODE"] == "polling"
    assert "polling" not in json.dumps(env.calls())


def test_render_writes_a_mode_600_file_and_refuses_partial_stores(env):
    out = env.path / "run" / "mavis.env"
    extra = {"MAVIS_ENV_FILE": str(out), "MAVIS_RENDER_ALLOW_NONROOT": "1"}
    env.store({"ENV": "prod"})
    proc = env.run("secrets.sh", "render", **extra)
    assert proc.returncode != 0 and not out.exists() and "required parameters missing" in proc.stderr
    env.store({**SECRETS, "ENV": "prod", "MACHINE_USERS": "[]"})
    proc = env.run("secrets.sh", "render", **extra)
    assert proc.returncode == 0, proc.stderr
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert stat.S_IMODE(out.parent.stat().st_mode) == 0o700
    lines = out.read_text().splitlines()
    assert "NATIVE_TOKEN_KEK=kek-VALUE-4444=" in lines and "ENV=prod" in lines
    no_values(proc, SECRETS.values())
    assert list(out.parent.iterdir()) == [out]  # temp dir cleaned
    # an SSM failure leaves the previous file untouched
    before = out.read_text()
    (env.path / "bin" / "aws").write_text("#!/bin/sh\nexit 255\n")
    proc = env.run("secrets.sh", "render", **extra)
    assert proc.returncode != 0 and out.read_text() == before


def test_render_requires_root_by_default(env):
    if os.getuid() == 0:
        pytest.skip("running as root")
    proc = env.run("secrets.sh", "render", MAVIS_ENV_FILE=str(env.path / "x" / "m.env"))
    assert proc.returncode != 0 and "root" in proc.stderr


def _deploy(env, local_env, **extra):
    return env.run("deploy.sh", "--no-webhook", DEMO_ENV=str(local_env), **extra)


def test_deploy_preserves_generated_secrets_from_ssm_and_uses_env_file(env):
    env.store(dict(SECRETS))
    local = write_env(env.path / "local.env", **OWNER)
    proc = _deploy(env, local)
    assert proc.returncode == 0, proc.stderr
    st = env.store()
    for k, v in SECRETS.items():
        assert st[k] == v
    assert st["ENV"] == "prod" and st["TELEGRAM_MODE"] == "polling"
    assert st["OLLAMA_API_KEY"] == OWNER["OLLAMA_API_KEY"]
    assert not set(SECRETS) & set(env.puts())
    ssh = env.ssh()
    assert "--env-file /run/mavis/mavis.env" in ssh and "mavis-secrets render" in ssh
    assert "docker compose -f" not in ssh
    no_values(proc, [*SECRETS.values(), *OWNER.values()])
    assert ".env.new" not in ssh.replace("rm -f /opt/mavis/.env.new", "")  # no plaintext upload


def test_deploy_generates_secrets_only_on_a_fresh_install(env):
    local = write_env(env.path / "local.env", **OWNER)
    proc = _deploy(env, local)
    assert proc.returncode == 0, proc.stderr
    assert set(SECRETS) <= set(env.store())
    kek = env.store()["NATIVE_TOKEN_KEK"]
    env.reset()
    assert _deploy(env, local).returncode == 0
    assert env.store()["NATIVE_TOKEN_KEK"] == kek and not set(SECRETS) & set(env.puts())


def test_deploy_refuses_to_generate_a_new_kek(env):
    local = write_env(env.path / "local.env", **OWNER)
    env.store({"OLLAMA_API_KEY": "x"})  # populated store without a KEK
    proc = _deploy(env, local)
    assert proc.returncode != 0 and "NATIVE_TOKEN_KEK" in proc.stderr and env.puts() == []
    # legacy box .env, empty SSM: seed first
    env.store({})
    box = write_env(env.path / "box.env", **SECRETS)
    proc = _deploy(env, local, STUB_BOX_ENV=str(box))
    assert proc.returncode != 0 and "seed-from-box" in proc.stderr and env.puts() == []


def test_deploy_removes_the_plaintext_env_only_after_the_switch(env):
    local = write_env(env.path / "local.env", **OWNER)
    box = write_env(env.path / "box.env", **SECRETS, ADMIN_PASSWORD="adm-VALUE")
    env.store(dict(SECRETS))  # KEK present but ADMIN_PASSWORD not yet seeded
    proc = _deploy(env, local, STUB_BOX_ENV=str(box))
    assert proc.returncode != 0 and "ADMIN_PASSWORD" in proc.stderr and "shred" not in env.ssh()
    env.store({**SECRETS, "ADMIN_PASSWORD": "adm-VALUE"})
    proc = _deploy(env, local, STUB_BOX_ENV=str(box))
    assert proc.returncode == 0, proc.stderr
    assert "shred -u /opt/mavis/.env" in env.ssh()
    no_values(proc, [*SECRETS.values(), "adm-VALUE"])


def test_iam_policy_is_least_privilege(env):
    proc = env.run("iam-role.sh", MAVIS_SKIP_PROPAGATION_WAIT="1")
    assert proc.returncode == 0, proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if "mavis-secrets-read" in ln)
    doc = line.split("--policy-document ", 1)[1]
    policy = json.loads(doc)
    stmts = {s["Sid"]: s for s in policy["Statement"]}
    read = stmts["ReadMavisParameters"]
    assert set(read["Action"]) == {"ssm:GetParametersByPath", "ssm:GetParameters", "ssm:GetParameter"}
    arn = "arn:aws:ssm:ap-south-1:276307603629:parameter/mavis/prod"
    assert all(r.startswith(arn) for r in read["Resource"])
    kms = stmts["DecryptViaSsmOnly"]
    assert kms["Action"] == ["kms:Decrypt"]
    assert kms["Condition"] == {"StringEquals": {"kms:ViaService": "ssm.ap-south-1.amazonaws.com"}}
    actions = [a for s in policy["Statement"] for a in s["Action"]]
    assert not [a for a in actions if a.startswith(("ssm:Put", "ssm:Delete", "ssm:Describe"))]
    assert len(policy["Statement"]) == 2 and not any(s["Effect"] != "Allow" for s in policy["Statement"])


def test_scripts_compose_calls_always_pass_the_env_file():
    for f in sorted(AWS_DIR.glob("*.sh")) + [ROOT / "docker-compose.prod.yml"]:
        for n, ln in enumerate(f.read_text().splitlines(), 1):
            code = "docker compose" in ln and not ln.lstrip().startswith("#")
            if code and "docker compose version" not in ln:
                assert "--env-file" in ln or "COMPOSE_BOX" in ln, f"{f.name}:{n}: {ln.strip()}"
