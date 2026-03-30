package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"strings"
)

type PRArgs struct {
	Repo string `json:"repo,omitempty"`
	PR   string `json:"pr,omitempty"`
	Diff string `json:"diff,omitempty"`
}

func main() {
	// 1. Read input from stdin
	input, err := io.ReadAll(os.Stdin)
	if err != nil {
		fatalError(fmt.Sprintf("Failed to read stdin: %v", err))
	}

	var args PRArgs
	if err := json.Unmarshal(input, &args); err != nil {
		fatalError(fmt.Sprintf("Failed to parse JSON args: %v", err))
	}

	if args.Diff == "" && (args.Repo == "" || args.PR == "") {
		fatalError("Missing required fields: must provide either 'diff' OR ('repo' and 'pr')")
	}

	diff := args.Diff

	if diff == "" {
		// 2. Fetch the diff natively via gh cli
		cmd := exec.Command("gh", "pr", "diff", args.PR, "--repo", args.Repo)
		var diffOut, diffErr bytes.Buffer
		cmd.Stdout = &diffOut
		cmd.Stderr = &diffErr

		if err := cmd.Run(); err != nil {
			fatalError(fmt.Sprintf("Error running gh pr diff: %s (stderr: %s)", err.Error(), diffErr.String()))
		}

		diff = strings.TrimSpace(diffOut.String())
	}

	if diff == "" {
		fmt.Println("Empty diff. Skipping review.")
		return
	}

	// 3. Analyze the diff
	critique := analyzeDiff(diff)

	// 4. Submit the review
	if args.Repo != "" && args.PR != "" {
		submitReview(args.Repo, args.PR, critique)
		if critique == "APPROVED" {
			fmt.Printf("Meta-Critic execution completed successfully for %s PR %s\nStatus: APPROVED\n", args.Repo, args.PR)
		} else {
			fmt.Printf("Meta-Critic execution completed successfully for %s PR %s\nStatus: **Meta-Critic Agent Triggered**\nCritique: %s\n", args.Repo, args.PR, critique)
		}
	} else {
		fmt.Printf("Meta-Critic execution completed locally. Critique:\n%s\n", critique)
	}
}

func analyzeDiff(diff string) string {
	apiKey := os.Getenv("LLM_API_KEY")
	if apiKey == "" {
		apiKey = os.Getenv("OPENAI_API_KEY")
	}
	if apiKey != "" || os.Getenv("LLM_BASE_URL") != "" {
		return analyzeWithLLM(diff, apiKey)
	}
	return generateMockCritique(diff)
}

func analyzeWithLLM(diff, apiKey string) string {
	prompt := fmt.Sprintf(`You are an Elite Agentic Code Reviewer acting as a "Meta-Critic". Evaluate this Git pull request diff against the following standards:
- The Netflix Standard: No silent exception suppression (no bare 'pass' blocks). Explicitly log exceptions.
- The OWASP Standard: Assume all inputs are malicious. Sanitize data.
- The Google Standard: Optimize for readability and strict formatting.

Pull Request Diff:
`+"```diff\n%s\n```"+`

If you find ANY violations, provide a concise critique requesting changes. 
If the code is flawless, respond exactly with "APPROVED".
Be highly technical and succinct.`, diff)

	model := os.Getenv("LLM_MODEL")
	if model == "" {
		model = "gpt-4o-mini"
	}

	url := "https://api.openai.com/v1/chat/completions"
	if os.Getenv("LLM_PROVIDER") == "gemini" {
		url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
	}
	if baseURL := os.Getenv("LLM_BASE_URL"); baseURL != "" {
		url = strings.TrimRight(baseURL, "/") + "/v1/chat/completions"
	}

	reqBody := map[string]interface{}{
		"model": model, 
		"messages": []map[string]string{
			{"role": "system", "content": "You are a senior engineer PR reviewer."},
			{"role": "user", "content": prompt},
		},
		"temperature": 0.2,
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return fmt.Sprintf("CRITIQUE BLOCKED: %v", err)
	}

	req, err := http.NewRequest("POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return fmt.Sprintf("CRITIQUE BLOCKED: %v", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+apiKey)

	client := &http.Client{}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Sprintf("CRITIQUE BLOCKED: LLM API failure: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		bodyBytes, _ := io.ReadAll(resp.Body)
		return fmt.Sprintf("CRITIQUE BLOCKED: API returned status %d: %s", resp.StatusCode, string(bodyBytes))
	}

	var resData struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}

	if err := json.NewDecoder(resp.Body).Decode(&resData); err != nil {
		return fmt.Sprintf("CRITIQUE BLOCKED: unpack error: %v", err)
	}

	if len(resData.Choices) > 0 {
		return strings.TrimSpace(resData.Choices[0].Message.Content)
	}
	return "CRITIQUE BLOCKED: Empty response from LLM"
}

func generateMockCritique(diff string) string {
	if strings.Contains(diff, "pass") || strings.Contains(diff, "except Exception:") || strings.Contains(diff, "panic(") || strings.Contains(diff, "recover(") {
		return "- **Netflix Standard Violation**: Detected silent/unhandled error handling blocking observability. Please explicitly log exceptions."
	}
	return "APPROVED"
}

func submitReview(repo, pr, critique string) {
	var cmd *exec.Cmd
	if critique == "APPROVED" {
		cmd = exec.Command("gh", "pr", "review", pr, "--repo", repo, "--approve", "-b", "**Meta-Critic Verification:** Code conforms to Elite Engineering standards.")
	} else {
		comment := fmt.Sprintf("**Meta-Critic Agent Triggered**\n\nThe following standards violations were detected in your code diff:\n\n%s", critique)
		cmd = exec.Command("gh", "pr", "review", pr, "--repo", repo, "--comment", "-b", comment)
	}

	var outErr bytes.Buffer
	cmd.Stderr = &outErr
	if err := cmd.Run(); err != nil {
		fatalError(fmt.Sprintf("Failed to submit review: %v (stderr: %s)", err, outErr.String()))
	}
}

func fatalError(msg string) {
	fmt.Fprintf(os.Stderr, "FATAL: %s\n", msg)
	os.Exit(1)
}
