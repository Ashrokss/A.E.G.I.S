"""Every path that holds durable state is env-overridable, and the default is
unchanged so local compose behaviour is identical.

The invariant that bites: DB_PATH is declared in several modules. If they ever
diverge, two components silently open two different SQLite files and runs
vanish half the time.

The api keeps NO file-based state: users, AWS accounts and integration
connections are all PostgreSQL rows (api/migrations/), owner-scoped in SQL.
Run: python -m tests.test_state_paths"""
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = ROOT.parent

# --- every agent DB_PATH declaration must be byte-identical -----------------
EXPECTED_DB = 'DB_PATH = os.getenv("AGENT_DB_PATH", "runs.db")'
DB_FILES = ["main.py", "agent.py", "kb_chat_agent.py"]

found = {}
for rel in DB_FILES:
    lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
    decls = [ln for ln in lines if ln.startswith("DB_PATH = ")]
    assert len(decls) == 1, f"{rel}: expected 1 DB_PATH decl, got {len(decls)}"
    found[rel] = decls[0]

for rel, decl in found.items():
    assert decl.startswith(EXPECTED_DB), f"{rel}: {decl!r} does not match {EXPECTED_DB!r}"

# No module outside the list may declare its own.
extra = [
    str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
    if ".deps" not in p.parts and "tests" not in p.parts
    and str(p.relative_to(ROOT)).replace(os.sep, "/") not in DB_FILES
    and any(ln.startswith("DB_PATH = ") for ln in p.read_text(encoding="utf-8").splitlines())
]
assert not extra, f"undeclared DB_PATH owners: {extra}"


# --- the terraform workspace dir honours its override, default unchanged ----
def workspace_dir(env_value):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")])}
    env.pop("AGENT_WORKSPACE_DIR", None)
    if env_value is not None:
        env["AGENT_WORKSPACE_DIR"] = env_value
    out = subprocess.run(
        [sys.executable, "-c",
         "from tools.terraform import TerraformTool; print(TerraformTool().base_workspace_dir)"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


assert workspace_dir(None) == "data/runs", f"default changed: {workspace_dir(None)!r}"
assert workspace_dir("/mnt/state/runs") == "/mnt/state/runs"

# --- the api has no file-based stores left ----------------------------------
api_src = REPO / "api" / "src"
for path in api_src.rglob("*.js"):
    text = path.read_text(encoding="utf-8")
    assert "BACKEND_DATA_DIR" not in text, f"{path}: file-based state is gone; use PostgreSQL"
    assert "writeFileSync" not in text, f"{path}: the api must not persist to local files"

# --- PostgreSQL is configured in exactly ONE module -------------------------
pg = (api_src / "db" / "pool.js").read_text(encoding="utf-8")
for var in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER"):
    assert f"env.{var}" in pg, f"db/pool.js: {var} is not read from the environment"

pool_decls = [p.name for p in api_src.rglob("*.js") if "new Pool(" in p.read_text(encoding="utf-8")]
assert pool_decls == ["pool.js"], f"exactly one module may construct the pg Pool, found: {pool_decls}"

print(f"ok: {len(DB_FILES)} DB_PATH decls agree, workspace override honoured, "
      "api has no file stores, PostgreSQL configured in exactly one module")
