import argparse
import subprocess
import os
import sys
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
try:
    from utils.logger import get_logger
    logger = get_logger(__name__)
except ImportError:
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

def estimate_tokens(text):
    # Rough estimation: 1 token ~= 4 chars typically
    return len(text) // 4

def main():
    parser = argparse.ArgumentParser(description="Flume Meta-Critic PR Reviewer (Native Binding)")
    parser.add_argument("--github-pr", required=True, help="GitHub PR Number to critique")
    args = parser.parse_args()
    
    pr_num = args.github_pr
    logger.info(f"Initiating Meta-Critic Native Evaluation for PR #{pr_num}")
    
    # 1. Fetch diff natively to calculate the "before" token usage
    diff_output = ""
    try:
        diff_output = subprocess.check_output(
            ["gh", "pr", "diff", str(pr_num)], 
            text=True, 
            stderr=subprocess.DEVNULL
        )
    except Exception as e:
        logger.warning(f"Failed to fetch PR diff natively: {e}")
        
    old_prompt = f"""
You are the Flume Meta-Critic, a senior Netflix/Google-tier Staff Engineer. 
Review the following PR diff. Focus critically on:
1. Python architectural best practices and zero-dependency compliance.
2. Code isolation and functional purity.
3. Observability and telemetry standards (structured JSON logging).
4. Edge cases or security vulnerabilities.

Output your code review strictly in GitHub-flavored markdown.
CRITICAL INSTRUCTION: Your entire response must be strictly in English. Do NOT output any Russian, Cyrillic, or other foreign language characters under any circumstances.

DIFF:
```diff
{diff_output[:15000]}
```
"""
    
    old_token_est = estimate_tokens(old_prompt)
    logger.info(f"[TELEMETRY] Legacy Prompt-Based Reviewer Estimated Tokens: ~{old_token_est} tokens")

    cwd = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))

    # 2. Run the native Go binary (it handles the GH interactions natively)
    logger.info("[TELEMETRY] Firing native Go meta-critic parser...")
    try:
        payload = json.dumps({"pr": pr_num, "repo": "Fremen-Labs/flume", "diff": diff_output})
        result = subprocess.run(
            ["go", "run", "cmd/flume/skills/meta-critic/main.go"],
            input=payload,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True
        )
        logger.info(result.stdout.strip())
        
        # Calculate new token metrics
        new_token_est = 0
        if "CRITIQUE BLOCKED" in result.stdout:
             logger.warning("LLM Fallback failed or blocked.")
        elif "APPROVED" not in result.stdout and "Meta-Critic Agent Triggered" in result.stdout:
             # Assume LLM or Heuristics fired. If LLM fired, it uses a much smaller prompt
             new_prompt = f"""You are an Elite Agentic Code Reviewer acting as a "Meta-Critic". Evaluate this Git pull request diff against the following standards:
- The Netflix Standard: No silent exception suppression (no bare 'pass' blocks). Explicitly log exceptions.
- The OWASP Standard: Assume all inputs are malicious. Sanitize data.
- The Google Standard: Optimize for readability and strict formatting.

Pull Request Diff:
```diff
{diff_output}
```

If you find ANY violations, provide a concise critique requesting changes. 
If the code is flawless, respond exactly with "APPROVED".
Be highly technical and succinct."""
             new_token_est = estimate_tokens(new_prompt)
             logger.info(f"[TELEMETRY] Native LLM-Fallback Token Overhad: ~{new_token_est} tokens (Saved {max(0, old_token_est - new_token_est)} tokens)")
        else:
             # Pure structural heuristics applied natively (0 tokens)
             logger.info(f"[TELEMETRY] Pure Native execution. Tokens Used: 0. Saved {old_token_est} tokens per invocation.")
             
    except subprocess.CalledProcessError as e:
        logger.error(f"Native Meta-Critic crashed: {e.stderr}")
        sys.exit(1)

    logger.info(f"Review successfully orchestrated by Native Go boundary for PR #{pr_num}")

if __name__ == "__main__":
    main()
