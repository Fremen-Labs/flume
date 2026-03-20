#!/usr/bin/env python3
"""
FLUME NEURAL CORTEX INSTALLER (NEXUS)
A beautiful, autonomous, Sci-Fi themed installation wizard.
Dynamically scans localhost to navigate matrix dependencies (OpenBao, Elastic, Elastro)
and queries the local Exo Cluster for procedural cyberpunk flavor text.
"""

import sys
import os
import shutil
import time
import socket
import urllib.request
import json
import subprocess

GREEN = '\033[92m'
CYAN = '\033[96m'
RED = '\033[91m'
YELLOW = '\033[93m'
BOLD = '\033[1m'
NC = '\033[0m'

def type_text(text, delay=0.015):
    for char in text:
        sys.stdout.write(char)
        sys.stdout.flush()
        time.sleep(delay)
    print()

def generate_exo_flavor():
    """Query the local Exo cluster to procedurally generate a cool loading phrase!"""
    try:
        req = urllib.request.Request(
            "http://localhost:52415/v1/chat/completions",
            data=json.dumps({
                "model": "mlx-community/Qwen3-30B-A3B-4bit",
                "messages": [{"role": "user", "content": "Write exactly ONLY one brief, edgy, short cyberpunk sci-fi computer loading string. No quotes, no markdown, no conversational text."}],
                "max_tokens": 50,
                "temperature": 0.8
            }).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        # Using a strict 5 second timeout. If the Mac Mini cluster is busy, we rollback to hardcoded strings seamlessly.
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode('utf-8'))
            return result['choices'][0]['message']['content'].strip()
    except Exception as e:
        sys.stderr.write(f"\\033[91m[LOG] Exo generation failed: {e}\\033[0m\\n")
        return ">>> SECURE UPLINK ESTABLISHED. BYPASSING CORRUPTED MAINFRAMES... <<<"

def print_banner():
    # ANSI Shadow Art 
    banner = f"""
{CYAN}{BOLD}
    ███████╗██╗     ██╗   ██╗████████╗███████╗
    ██╔════╝██║     ██║   ██║██╔═════╝██╔════╝
    █████╗  ██║     ██║   ██║██████╗  █████╗
    ██╔══╝  ██║     ██║   ██║██╔═══╝  ██╔══╝
    ██║     ███████╗╚██████╔╝████████╗███████╗
    ╚═╝     ╚══════╝ ╚═════╝ ╚═══════╝╚══════╝
{NC}
{YELLOW}{BOLD}{generate_exo_flavor()}{NC}
"""
    print(banner)
    type_text(f"{GREEN}[SYS] Initializing Flume Neural Cortex Installer Protocol...{NC}")

def check_port(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0

def scan_infrastructure():
    infra = {}
    type_text(f"\n{CYAN}--- RUNNING SECTOR DIAGNOSTICS ---{NC}")
    
    # 1. Check Elasticsearch Core
    sys.stdout.write(f"{CYAN}[SCAN]{NC} Probing local matrix for Elastic core [Port 9200]... ")
    sys.stdout.flush()
    time.sleep(0.5)
    if check_port('127.0.0.1', 9200):
        print(f"{GREEN}[DETECTED]{NC}")
        infra['elastic'] = True
    else:
        print(f"{RED}[MISSING]{NC}")
        infra['elastic'] = False

    # 2. Check OpenBao Security Vault
    sys.stdout.write(f"{CYAN}[SCAN]{NC} Locating OpenBao Secure Vault binaries... ")
    sys.stdout.flush()
    time.sleep(0.5)
    if shutil.which('openbao'):
        print(f"{GREEN}[DETECTED]{NC}")
        infra['openbao'] = True
    else:
        print(f"{RED}[MISSING]{NC}")
        infra['openbao'] = False

    # 3. Check Elastro AST Integration
    sys.stdout.write(f"{CYAN}[SCAN]{NC} Sweeping memory banks for Elastro endpoints... ")
    sys.stdout.flush()
    time.sleep(0.5)
    if shutil.which('elastro'):
        print(f"{GREEN}[DETECTED]{NC}")
        infra['elastro'] = True
    else:
        print(f"{RED}[MISSING]{NC}")
        infra['elastro'] = False
        
    return infra

def ask_choice(prompt, options):
    print(f"\n{BOLD}{prompt}{NC}")
    for idx, opt in enumerate(options, 1):
        print(f"  {CYAN}[{idx}]{NC} {opt}")
    while True:
        choice = input(f"\n{YELLOW}root@flume-cortex:~# {NC}").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return int(choice)
        print(f"{RED}Invalid override sequence. Specify a valid numerical trajectory.{NC}")

def deploy_flume(mode):
    type_text(f"\n{BOLD}{CYAN}>>> DEPLOYING FLUME CORE IN [{mode}] MODE... <<<{NC}")
    
    # Delegate to the legacy setup.sh but inject our resolved parameters
    env = os.environ.copy()
    if mode == "ASSIMILATION":
        env["FLUME_SKIP_ELASTIC_INSTALL"] = "true"
        env["FLUME_EXT_OPENBAO"] = "true"
        
    script_path = os.path.join(os.path.dirname(__file__), 'setup.sh')
    if os.path.exists(script_path):
        try:
            subprocess.call(["bash", "setup.sh"], env=env)
        except KeyboardInterrupt:
            print(f"\n{RED}[SYS] Neural upload forcefully terminated by user.{NC}")
            sys.exit(1)
    else:
        print(f"{RED}[CRITICAL] setup.sh module missing. Hive execution failed.{NC}")
        sys.exit(1)

def main():
    os.system('clear' if os.name == 'posix' else 'cls')
    print_banner()
    infra = scan_infrastructure()
    
    print(f"\n{BOLD}========================================================================{NC}")
    
    if any(infra.values()):
        type_text(f"{YELLOW}[WARNING] Pre-existing infrastructure detected bridging this sector.{NC}")
        type_text(f"To prevent cascading timeline implosions, please define the integration matrix:\n")
        
        choice = ask_choice(f"MATRIX INTEGRATION PATHS:", [
            f"{GREEN}ASSIMILATION{NC} -> Bind Flume to the pre-existing host infrastructure (Utilize current standalone ports).",
            f"{CYAN}ISOLATION{NC}    -> Ignore pre-existing daemons. Construct a localized, independent Flume swarm container.",
            f"{RED}ABORT{NC}        -> Terminate Neural Cortex sequence."
        ])
        
        if choice == 1:
            type_text(f"\n{GREEN}[ACKNOWLEDGED] Trajectory: ASSIMILATION.{NC}")
            type_text(f"Synchronizing current host daemons into the Flume neural net...")
            deploy_flume("ASSIMILATION")
        elif choice == 2:
            type_text(f"\n{CYAN}[ACKNOWLEDGED] Trajectory: ISOLATION.{NC}")
            type_text(f"Spawning completely isolated dockerized swarm instances...")
            deploy_flume("ISOLATION")
        else:
            type_text(f"\n{RED}Installation Aborted. Jacking out of the matrix.{NC}")
            sys.exit(0)
    else:
        type_text(f"{CYAN}[SYSTEM] Sector clear. Preparing for completely fresh Flume Hive Swarm deployment.{NC}")
        choice = ask_choice(f"DEPLOYMENT AUTHORIZATION:", [
            f"{GREEN}ENGAGE{NC} -> Initiate full Flume Swarm (Elastic, OpenBao, Web UI)",
            f"{RED}ABORT{NC}  -> Terminate Neural Cortex sequence."
        ])
        
        if choice == 1:
            deploy_flume("FRESH")
        else:
            sys.exit(0)

if __name__ == "__main__":
    main()
