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
import getpass

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

def check_dashboard_port():
    DEFAULT_PORT = 8765
    type_text(f"\n{CYAN}--- ORBITAL PORT CHECK ---{NC}")
    sys.stdout.write(f"{CYAN}[SCAN]{NC} Sweeping comms port {DEFAULT_PORT}... ")
    sys.stdout.flush()
    time.sleep(0.5)
    
    if check_port('127.0.0.1', DEFAULT_PORT):
        print(f"{RED}[COLLISION DETECTED]{NC}")
        type_text(f"\n{YELLOW}[CRITICAL ANOMALY] Port {DEFAULT_PORT} is monopolized by a rogue process.{NC}")
        choice = ask_choice(f"PORT OVERRIDE PROTOCOL:", [
            f"{GREEN}AUTO-ASSIGN{NC} -> Calculate available fallback subspace frequency",
            f"{CYAN}MANUAL{NC}      -> Enter custom port authorization override"
        ])
        if choice == 1:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('', 0))
                new_port = s.getsockname()[1]
            type_text(f"{GREEN}[SUCCESS] Subspace frequency securely calculated on Port {new_port}{NC}")
            return new_port
        else:
            while True:
                user_port = input(f"\n{YELLOW}root@flume-cortex:~# Enter override port (1024-65535): {NC}").strip()
                if user_port.isdigit() and 1024 <= int(user_port) <= 65535:
                    if not check_port('127.0.0.1', int(user_port)):
                        type_text(f"{GREEN}[ACCEPTED] Port {user_port} manually locked.{NC}")
                        return int(user_port)
                    else:
                        print(f"{RED}[ERROR] Port {user_port} is also monopolized. Try another frequency.{NC}")
                else:
                    print(f"{RED}[ERROR] Invalid sequence. Must be between 1024 and 65535.{NC}")
    else:
        print(f"{GREEN}[CLEAR]{NC}")
        type_text(f"{GREEN}Port {DEFAULT_PORT} is available for Flume Hive bindings.{NC}")
        return DEFAULT_PORT

def detect_hardware():
    cpu_cores = os.cpu_count() or 4
    try:
        if sys.platform == 'darwin':
            ram_bytes = int(subprocess.check_output(['sysctl', '-n', 'hw.memsize']))
            ram_gb = ram_bytes // (1024**3)
            try:
                gpu_info = subprocess.check_output(['system_profiler', 'SPDisplaysDataType']).decode(errors='ignore')
                gpu_name = "Apple Silicon GPU"
                for line in gpu_info.splitlines():
                    if "Chipset Model:" in line:
                        gpu_name = line.split(":", 1)[1].strip()
                        break
            except Exception:
                gpu_name = "Unknown macOS GPU"
        elif sys.platform.startswith('linux'):
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    if 'MemTotal' in line:
                        ram_gb = int(line.split()[1]) // (1024**2)
                        break
            try:
                gpu_info = subprocess.check_output(['nvidia-smi', '--query-gpu=name', '--format=csv,noheader', '-i', '0']).decode(errors='ignore').strip()
                gpu_name = gpu_info if gpu_info else "Unknown Linux GPU"
            except Exception:
                gpu_name = "No NVIDIA GPU Detected"
        else:
            ram_gb = 8
            gpu_name = "Unknown GPU"
    except Exception:
        ram_gb = 8
        gpu_name = "Unknown GPU"
        
    return cpu_cores, ram_gb, gpu_name

def append_to_env(port):
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    cpu_cores, ram_gb, gpu_name = detect_hardware()
    type_text(f"{GREEN}[HARDWARE SCAN]{NC} Detected {cpu_cores} Cores, {ram_gb}GB RAM, [{gpu_name}]")
    try:
        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                lines = f.readlines()
            lines = [line for line in lines if not any(line.startswith(p) for p in ('DASHBOARD_PORT=', 'HOST_CPU_CORES=', 'HOST_RAM_GB=', 'HOST_GPU_NAME='))]
            lines.append(f"DASHBOARD_PORT={port}\n")
            lines.append(f"HOST_CPU_CORES={cpu_cores}\n")
            lines.append(f"HOST_RAM_GB={ram_gb}\n")
            lines.append(f"HOST_GPU_NAME={gpu_name}\n")
            with open(env_path, 'w') as f:
                f.writelines(lines)
        else:
            with open(env_path, 'w') as f:
                f.write(f"DASHBOARD_PORT={port}\n")
                f.write(f"HOST_CPU_CORES={cpu_cores}\n")
                f.write(f"HOST_RAM_GB={ram_gb}\n")
                f.write(f"HOST_GPU_NAME={gpu_name}\n")
        type_text(f"{CYAN}[SYS] Neural configurations successfully patched with port {port} and hardware telemetry.{NC}")
    except Exception as e:
        sys.stderr.write(f"\n\033[91m[LOG] Failed to sync override port to .env: {e}\033[0m\n")

def inject_elastic_credentials(infra):
    if not infra.get('openbao'):
        type_text(f"\\n{RED}[CRITICAL ERROR] OpenBao installation not detected locally.{NC}")
        type_text(f"To securely assimilate Elastic credentials, please manually establish the OpenBao matrix first.{NC}")
        sys.exit(1)

    type_text(f"\\n{CYAN}--- ELASTIC CREDENTIAL ASSIMILATION ---{NC}")
    type_text(f"{YELLOW}A local Elasticsearch instance was detected. Please provide your API Key to securely bind it into the OpenBao matrix.{NC}")
    es_key = getpass.getpass(f"{BOLD}root@flume-cortex:~# ES_API_KEY: {NC}").strip()
    
    if not es_key:
        type_text(f"{RED}[ERROR] Blank token supplied. Neural upload aborted.{NC}")
        sys.exit(1)

    sys.stdout.write(f"{CYAN}[SYS]{NC} Transmitting token to OpenBao vault (secret/flume_elastic)... ")
    sys.stdout.flush()
    time.sleep(0.5)

    try:
        proc = subprocess.run(
            ["openbao", "kv", "put", "-format=json", "secret/flume_elastic", f"ES_API_KEY={es_key}"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if proc.returncode == 0:
            print(f"{GREEN}[SECURED]{NC}")
            type_text(f"{GREEN}Elasticsearch credentials locked permanently into the OpenBao hive.{NC}")
        else:
            print(f"{RED}[FAILED]{NC}")
            sys.stderr.write(f"\\n\\033[91m[LOG] OpenBao KV Error: {proc.stderr}\\033[0m\\n")
            sys.exit(1)
    except Exception as e:
        print(f"{RED}[FAILED]{NC}")
        sys.stderr.write(f"\\n\\033[91m[LOG] Subprocess execution threw anomaly: {e}\\033[0m\\n")
        sys.exit(1)

def deploy_flume(mode, selected_port):
    type_text(f"\n{BOLD}{CYAN}>>> DEPLOYING FLUME CORE IN [{mode}] MODE... <<<{NC}")
    
    append_to_env(selected_port)
    env = os.environ.copy()
    env["DASHBOARD_PORT"] = str(selected_port)
    
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
            if infra.get('elastic'):
                inject_elastic_credentials(infra)
            assigned_port = check_dashboard_port()
            deploy_flume("ASSIMILATION", assigned_port)
        elif choice == 2:
            type_text(f"\n{CYAN}[ACKNOWLEDGED] Trajectory: ISOLATION.{NC}")
            type_text(f"Spawning completely isolated dockerized swarm instances...")
            assigned_port = check_dashboard_port()
            deploy_flume("ISOLATION", assigned_port)
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
            assigned_port = check_dashboard_port()
            deploy_flume("FRESH", assigned_port)
        else:
            sys.exit(0)

if __name__ == "__main__":
    main()
