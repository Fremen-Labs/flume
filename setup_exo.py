#!/usr/bin/env python3
"""
setup_exo.py
A one-click CLI integration tool to securely bind a local Exo cluster into Flume's multi-agent workflow.
This script natively imports Flume's internal workspace APIs to guarantee schema compliance.
"""

import sys
import os
import argparse
from pathlib import Path

# Dynamically add the Flume src directory to PYTHONPATH for native API imports
FLUME_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(FLUME_ROOT / "src"))
sys.path.insert(0, str(FLUME_ROOT / "src" / "dashboard"))

try:
    import llm_credentials_store as lcs
    from dashboard import agent_models_settings as ams
except ImportError as e:
    print(f"❌ Failed to import internal Flume dependencies. Please ensure this script is run from the root of the flume repository: {e}")
    sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Flume-Exo Cluster Autonomous Integration CLI")
    parser.add_argument("--url", default="http://localhost:52415/v1", help="Exo Cluster API Base URL")
    parser.add_argument("--model", default="mlx-community/Qwen3-30B-A3B-4bit", help="Exo Model Identifier")
    parser.add_argument("--roles", nargs="+", default=["implementer", "reviewer"], help="Agent roles to seamlessly map to Exo")
    args = parser.parse_args()

    print(f"🚀 Initializing Exo Cluster Integration for Flume...")
    print(f"🔗 Target Exo URL: {args.url}")
    print(f"🧠 Target Model: {args.model}")

    try:
        # 1. Upsert the Exo Credential natively
        cred_id = "__exo_cluster__"
        label = "Mac Mini Exo Cluster"
        
        # We use the internal `upsert_credential` to safely generate or overwrite the llm_credentials.json record
        # without breaking existing integrations.
        lcs.upsert_credential(
            workspace_root=FLUME_ROOT,
            cred_id=cred_id,
            label=label,
            provider="openai_compatible",
            api_key="exo-local-dummy-key",
            base_url=args.url
        )
        print("✅ Successfully registered Exo Cluster as a native Flume LLM Provider.")

        # 2. Map the Agent Roles safely using the validation API
        roles_payload = {}
        for role in args.roles:
            roles_payload[role] = {
                "credentialId": cred_id,
                "provider": "openai_compatible",
                "model": args.model
            }
        
        # We ask Flume's inner dashboard validator to build the payload strictly so schema logic is isolated
        ok, err, new_data = ams.validate_save_agent_models(
            workspace_root=FLUME_ROOT,
            payload={"roles": roles_payload}
        )
        
        if not ok:
            print(f"❌ Failed to validate agent role mapping: {err}")
            sys.exit(1)
            
        ams.save_agent_models(FLUME_ROOT, new_data)
        print(f"✅ Successfully mapped roles {args.roles} to Exo.")
        print("\n🎉 Flume is now fully autonomously integrated with Exo! No further setup required.")
        
    except Exception as e:
        print(f"❌ Integration failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
