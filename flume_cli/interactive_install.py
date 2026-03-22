"""Interactive prompts for `./flume install` (TTY only)."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

import click

CredentialMode = Literal["openbao", "env", "skip"]

# Reuse CLI styling when used from flume_cli.cli
CYAN = "\033[0;36m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
BOLD = "\033[1m"
NC = "\033[0m"


def is_interactive_tty() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def run_credential_wizard(root: Path) -> CredentialMode:
    click.echo(f"\n{BOLD}Credential storage{NC}")
    click.echo(
        "Flume can load API keys and OAuth state from:\n"
        f"  {GREEN}1{NC}  OpenBao KV — recommended; secrets not stored in plain files in the repo\n"
        f"  {GREEN}2{NC}  A repo-root {GREEN}.env{NC} file — simple, but keep it out of git and backups you share\n"
        f"  {GREEN}3{NC}  Skip — you will configure later (see install/README.md)\n"
    )
    choice = click.prompt("Choose 1 / 2 / 3", type=str, default="1").strip().lower()
    if choice in ("3", "skip", "s"):
        click.echo(f"{CYAN}Skipping credential template.{NC}\n")
        return "skip"
    if choice in ("2", "env", "dotenv", "."):
        click.echo(
            f"{YELLOW}Using .env: add ES_URL, ES_API_KEY, and LLM settings to {root / '.env'} "
            f"(never commit secrets).{NC}\n"
        )
        return "env"
    # Default / OpenBao path
    example = root / "install" / "flume.config.example.json"
    target = root / "flume.config.json"
    if not target.is_file() and example.is_file():
        if click.confirm(
            f"Create {target.name} from the example (OpenBao address + KV path + token file path)?",
            default=True,
        ):
            shutil.copy2(example, target)
            click.echo(f"{GREEN}✔ Wrote {target}{NC}")
    click.echo(
        f"\n{CYAN}OpenBao next steps:{NC}\n"
        "  • Put a long-lived token in the path referenced by tokenFile (chmod 600).\n"
        "  • Store ES_API_KEY, ES_URL, LLM_*, OPENAI_OAUTH_* in KV at the configured mount/path.\n"
        "  • With Docker: ./flume start brings up OpenBao on localhost:8200 (dev token in compose docs).\n"
        "  • If ES_URL still uses the Docker hostname `elasticsearch`, the installer remaps it and saves the\n"
        "    host URL to OpenBao / .env for you after indices succeed.\n"
        "  • New installs: provisioning can drop install/.es-bootstrap.env (API key) or set ELASTIC_PASSWORD /\n"
        "    install/.elastic-admin.env so the user never types passwords — Flume creates/saves keys automatically.\n"
    )
    return "openbao"


def _openbao_cli_available() -> bool:
    return bool(shutil.which("openbao"))


def elasticsearch_reachable() -> bool:
    """True if something responds on localhost Elasticsearch (HTTPS or HTTP)."""
    urls = (
        "https://localhost:9200/",
        "https://127.0.0.1:9200/",
        "http://localhost:9200/",
        "http://127.0.0.1:9200/",
    )
    for url in urls:
        try:
            proc = subprocess.run(
                [
                    "curl",
                    "-sk",
                    "-o",
                    "/dev/null",
                    "-w",
                    "%{http_code}",
                    "--connect-timeout",
                    "2",
                    "--max-time",
                    "5",
                    url,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            continue
        code = (proc.stdout or "").strip()
        if proc.returncode == 0 and code and code != "000":
            return True
    return False


def _run_sudo_setup_script(root: Path, *relative_parts: str) -> int:
    script = root.joinpath(*relative_parts)
    if not script.is_file():
        click.echo(f"{YELLOW}Missing {script}{NC}", err=True)
        return 1
    return subprocess.run(["sudo", "bash", str(script)], cwd=root).returncode


def ensure_platform_dependencies(root: Path, credential_mode: CredentialMode) -> None:
    """
    For interactive installs: offer to run root setup scripts so OpenBao + Elasticsearch
    match what new users need (CLI on PATH, local ES with bootstrap credentials).
    """
    if not is_interactive_tty():
        return
    if credential_mode == "skip":
        return

    if credential_mode == "openbao" and not _openbao_cli_available():
        click.echo(f"\n{BOLD}OpenBao CLI{NC}")
        click.echo(
            f"You chose OpenBao KV, but {GREEN}openbao{NC} is not on PATH. "
            "Flume uses the CLI to read secrets during index setup and tooling.\n"
        )
        if click.confirm(
            "Install the OpenBao CLI now (downloads the official binary; requires sudo)?",
            default=True,
        ):
            rc = _run_sudo_setup_script(root, "install", "setup", "install-openbao.sh")
            if rc != 0:
                click.echo(
                    f"{YELLOW}OpenBao CLI install exited {rc}. "
                    f"Install manually: {GREEN}sudo bash install/setup/install-openbao.sh{NC}{YELLOW}.{NC}\n",
                    err=True,
                )
            elif not _openbao_cli_available():
                click.echo(
                    f"{YELLOW}openbao still not on PATH — open a new shell or check install/setup/install-openbao.sh.{NC}\n",
                    err=True,
                )
            else:
                click.echo(f"{GREEN}✔ openbao CLI is available.{NC}\n")
        else:
            click.echo(
                f"{YELLOW}Skipped. Without the CLI, put ES_URL + ES_API_KEY in {GREEN}.env{YELLOW} "
                f"or install the CLI later.{NC}\n"
            )

    if credential_mode not in ("openbao", "env"):
        return

    if elasticsearch_reachable():
        return

    click.echo(f"\n{BOLD}Elasticsearch{NC}")
    click.echo(
        "Flume needs a running Elasticsearch 8 cluster. None was detected on localhost (ports 9200).\n"
        f"  {CYAN}•{NC} {GREEN}Install now{NC}: native package + TLS on https://localhost:9200, API key written to "
        f"{GREEN}install/.es-bootstrap.env{NC} (sudo).\n"
        f"  {CYAN}•{NC} {GREEN}Skip{NC}: use Docker Compose (`./flume start`) or your own cluster; set ES_URL / ES_API_KEY "
        "before indexing.\n"
    )
    if not click.confirm("Install Elasticsearch 8 on this machine now (requires sudo)?", default=True):
        click.echo(
            f"{YELLOW}Skipped native Elasticsearch. Start your cluster, then run {GREEN}./flume es-indices{NC}.{NC}\n"
        )
        return
    rc = _run_sudo_setup_script(root, "install", "setup", "install-elasticsearch.sh")
    if rc != 0:
        click.echo(
            f"{YELLOW}Elasticsearch installer exited {rc}. "
            f"Try manually: {GREEN}sudo bash install/setup/install-elasticsearch.sh{NC}{YELLOW}.{NC}\n",
            err=True,
        )
    elif not elasticsearch_reachable():
        click.echo(
            f"{YELLOW}Elasticsearch still not responding on localhost:9200 — check: "
            f"{GREEN}sudo systemctl status elasticsearch{NC}{YELLOW}.{NC}\n",
            err=True,
        )
    else:
        click.echo(f"{GREEN}✔ Elasticsearch is reachable.{NC}\n")

    if credential_mode == "openbao":
        click.echo(
            f"{CYAN}OpenBao server:{NC} the CLI does not start a server. Use `./flume start` (Docker dev stack) "
            "or run OpenBao elsewhere, then store ES_* and other secrets in KV (or rely on "
            f"{GREEN}install/.es-bootstrap.env{NC} until you do).\n"
        )


def prompt_run_es_indices(root: Path, credential_mode: CredentialMode) -> bool:
    """
    Ask whether to run create-es-indices.sh, with copy that matches the user's
    credential choice and whether the OpenBao CLI is installed.
    """
    has_env = (root / ".env").is_file()
    has_flume_config = (root / "flume.config.json").is_file()
    ob_cli = _openbao_cli_available()

    if not has_env and not has_flume_config:
        if credential_mode == "openbao":
            click.echo(
                f"{YELLOW}No flume.config.json or .env in the repo root yet — cannot load Elasticsearch "
                f"credentials. Add flume.config.json (and token file) or .env with ES_URL + ES_API_KEY, "
                f"then run:{NC} {GREEN}./flume es-indices{NC}\n"
            )
        elif credential_mode == "env":
            click.echo(
                f"{YELLOW}No .env file yet — create {root / '.env'} with ES_URL and ES_API_KEY, "
                f"then re-run {GREEN}./flume install{YELLOW} or run:{NC} {GREEN}./flume es-indices{NC}\n"
            )
        else:
            click.echo(
                f"{YELLOW}No .env or flume.config.json yet — skipping Elasticsearch index setup. "
                f"When credentials exist, run:{NC} {GREEN}./flume es-indices{NC}\n"
            )
        return False

    click.echo(f"\n{BOLD}Elasticsearch index setup{NC}")
    click.echo(
        "Flume stores plans, tasks, handoffs, and memory in Elasticsearch. "
        "This step applies the bundled index templates and creates (or updates) those indices "
        "so the dashboard and workers can read and write data.\n"
    )

    if credential_mode == "openbao":
        if ob_cli and has_flume_config:
            click.echo(
                f"{CYAN}You chose OpenBao and the {GREEN}openbao{CYAN} CLI is available.{NC} "
                "The script will load ES_URL and ES_API_KEY from OpenBao KV "
                f"(using {GREEN}flume.config.json{NC} and your token file), and merge any values from "
                f"{GREEN}.env{NC} if that file exists.\n"
            )
        elif has_flume_config and not ob_cli:
            click.echo(
                f"{YELLOW}You chose OpenBao and flume.config.json is present, but the {GREEN}openbao{YELLOW} "
                f"CLI was not found on PATH.{NC} Index creation needs either the CLI to read KV, or "
                f"ES_URL + ES_API_KEY already in your environment / {GREEN}.env{NC}. "
                "Install the OpenBao CLI or put those variables in .env, then try again.\n"
            )
        elif has_env and not has_flume_config:
            click.echo(
                f"{CYAN}You chose OpenBao but only {GREEN}.env{NC} exists here — credentials will be read from "
                f".env (OpenBao KV is not configured until flume.config.json + token are set up).\n"
            )
        else:
            click.echo(
                f"{CYAN}OpenBao-oriented setup:{NC} ensure ES_URL and ES_API_KEY are reachable via OpenBao KV "
                f"and/or {GREEN}.env{NC} before confirming.\n"
            )
    elif credential_mode == "env":
        click.echo(
            f"{CYAN}You chose plain {GREEN}.env{NC}.{NC} "
            f"The script reads {GREEN}ES_URL{NC} (your cluster URL, e.g. https://localhost:9200) and "
            f"{GREEN}ES_API_KEY{NC} from that file so it can connect and create indices.\n"
        )
    else:
        # User skipped the credential wizard; explain both paths
        parts = []
        if has_flume_config and ob_cli:
            parts.append(
                f"Found {GREEN}flume.config.json{NC} and the OpenBao CLI — KV values can be loaded for ES_*."
            )
        elif has_flume_config and not ob_cli:
            parts.append(
                f"Found {GREEN}flume.config.json{NC} but no OpenBao CLI on PATH — put ES_* in {GREEN}.env{NC} "
                "or install openbao."
            )
        if has_env:
            parts.append(f"{GREEN}.env{NC} will be sourced for any ES_* variables.")
        if parts:
            click.echo(f"{CYAN}{' '.join(parts)}{NC}\n")
        else:
            click.echo(
                f"{CYAN}Using whatever credential files are present ({GREEN}.env{NC} / OpenBao via "
                f"{GREEN}flume.config.json{NC}).\n"
            )

    return click.confirm(
        f"{BOLD}Run this Elasticsearch index setup step now?{NC}",
        default=True,
    )


def prompt_oauth_flow(root: Path, run_oauth_script) -> bool:
    """
    run_oauth_script: callable[[Path, tuple[str, ...]], int]  (e.g. _run_codex_oauth_script)

    Returns True if an OAuth subcommand was invoked (even if it exited non-zero).
    """
    if not click.confirm(
        "Set up OpenAI ChatGPT / Codex OAuth now (monthly subscription / Codex-style access)?",
        default=False,
    ):
        click.echo(f"{CYAN}Later: ./flume setup   or   ./flume codex-oauth login-browser{NC}\n")
        return False
    click.echo(
        f"\n{CYAN}Codex OAuth flows:{NC}\n"
        f"  {GREEN}b{NC}  login-browser — browser on this machine\n"
        f"  {GREEN}p{NC}  login-paste — headless / SSH (paste redirect URL)\n"
        f"  {GREEN}i{NC}  import-codex — import tokens from official Codex CLI (~/.codex/auth.json)\n"
        f"  {GREEN}s{NC}  skip\n"
    )
    sub = click.prompt("Choose b / p / i / s", type=str, default="b").strip().lower()
    args: tuple[str, ...] = ()
    if sub in ("b", "browser"):
        args = ("login-browser",)
    elif sub in ("p", "paste"):
        args = ("login-paste",)
    elif sub in ("i", "import", "import-codex"):
        args = ("import-codex",)
    else:
        click.echo(f"{CYAN}Skipped OAuth run.{NC}\n")
        return False
    click.echo("")
    rc = run_oauth_script(root, args)
    if rc != 0:
        click.echo(f"{YELLOW}OAuth helper exited {rc}.{NC}", err=True)
    return True
