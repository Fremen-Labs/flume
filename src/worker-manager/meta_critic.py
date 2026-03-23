import argparse
import subprocess
import os
import sys

# Ensure src/ is in the python path for utils importing
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from utils import llm_client
from utils.logger import get_logger

logger = get_logger(__name__)

def main():
    parser = argparse.ArgumentParser(description="Flume Meta-Critic PR Reviewer")
    parser.add_argument("--github-pr", required=True, help="GitHub PR Number to critique")
    args = parser.parse_args()
    
    pr_num = args.github_pr
    logger.info(f"Initiating Meta-Critic AI Review for PR #{pr_num}")
    
    try:
        # Fetch the diff natively using gh CLI
        diff_output = subprocess.check_output(
            ["gh", "pr", "diff", str(pr_num)], 
            text=True, 
            stderr=subprocess.DEVNULL
        )
    except Exception as e:
        logger.error(f"Failed to fetch PR diff for #{pr_num}: {e}")
        sys.exit(1)
        
    if not diff_output.strip():
        logger.info(f"PR #{pr_num} has no diff. Skipping review.")
        sys.exit(0)

    prompt = f"""
You are the Flume Meta-Critic, a senior Netflix/Google-tier Staff Engineer. 
Review the following PR diff. Focus critically on:
1. Python architectural best practices and zero-dependency compliance.
2. Code isolation and functional purity.
3. Observability and telemetry standards (structured JSON logging).
4. Edge cases or security vulnerabilities.

Output your code review strictly in GitHub-flavored markdown.

DIFF:
```diff
{diff_output[:15000]}
```
"""
    try:
        response = llm_client.chat([
            {"role": "system", "content": "You are a senior python architecture reviewer."},
            {"role": "user", "content": prompt}
        ])
        
        logger.info(f"Meta-Critic successfully rendered review for PR #{pr_num}")
        
        # Post the comment back to the PR
        subprocess.run(
            ["gh", "pr", "comment", str(pr_num), "--body", response],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        
        logger.info(f"Review successfully published to GitHub PR #{pr_num}")
    except Exception as e:
        logger.error(f"Meta-Critic pipeline evaluation failed: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
