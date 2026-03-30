package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"regexp"
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

	// 3. Optional Zero-LLM Linter First-Pass
	if lintErr, failed := runLinters(diff); failed {
		submitReview(args.Repo, args.PR, lintErr)
		fmt.Printf("Meta-Critic execution completed successfully for %s PR %s\nStatus: **Meta-Critic Agent Triggered (Linter Execution)**\nCritique: %s\n", args.Repo, args.PR, lintErr)
		return
	}

	// 4. Elastic Caching: Check if the exact structural diff was already evaluated
	diffHash := hashDiff(diff)
	if cachedCritique, ok := checkElasticCache(diffHash); ok {
		fmt.Printf("Meta-Critic execution resolved from Elastic Cache for %s PR %s\nStatus: APPROVED via Cache\nCritique: %s\n", args.Repo, args.PR, cachedCritique)
		return
	}

	// 5. Analyze the diff natively
	critique := analyzeDiff(diff)

	// 6. Save outcome to Elastic Cache
	saveElasticCache(diffHash, critique)

	// 7. Submit the review
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

func runLinters(diff string) (string, bool) {
	hasPy := strings.Contains(diff, ".py")
	hasGo := strings.Contains(diff, ".go")

	var violations []string

	if hasPy {
		cmd := exec.Command("ruff", "check", ".")
		var out bytes.Buffer
		cmd.Stdout = &out
		cmd.Stderr = &out
		if err := cmd.Run(); err != nil {
			msg := out.String()
			if msg == "" {
				msg = err.Error()
			}
			violations = append(violations, "### Ruff Python Linter Violations:\n```text\n"+strings.TrimSpace(msg)+"\n```")
		}
	}

	if hasGo {
		cmd := exec.Command("golangci-lint", "run", "./...")
		var out bytes.Buffer
		cmd.Stdout = &out
		cmd.Stderr = &out
		if err := cmd.Run(); err != nil {
			msg := out.String()
			if msg == "" {
				msg = err.Error()
			}
			violations = append(violations, "### GolangCI-Lint Violations:\n```text\n"+strings.TrimSpace(msg)+"\n```")
		}
	}

	if len(violations) > 0 {
		return "**Zero-LLM Linter Checks Failed:**\n\n" + strings.Join(violations, "\n\n"), true
	}
	return "", false
}

func sanitizeOutput(critique string) string {
	sanitized := strings.ReplaceAll(critique, "<script>", "&lt;script&gt;")
	sanitized = strings.ReplaceAll(sanitized, "</script>", "&lt;/script&gt;")
	sanitized = strings.ReplaceAll(sanitized, "curl ", "c&#117;rl ")
	sanitized = strings.ReplaceAll(sanitized, "wget ", "wg&#101;t ")
	sanitized = strings.ReplaceAll(sanitized, "rm -rf ", "rm -r&#102; ")
	sanitized = strings.ReplaceAll(sanitized, "os.system(", "os.syst&#101;m(")
	return sanitized
}

func analyzeDiff(diff string) string {
	apiKey := os.Getenv("LLM_API_KEY")
	if apiKey == "" {
		apiKey = os.Getenv("OPENAI_API_KEY")
	}
	if apiKey != "" || os.Getenv("LLM_BASE_URL") != "" {
		return sanitizeOutput(analyzeWithLLM(diff, apiKey))
	}
	return sanitizeOutput(generateMockCritique(diff))
}

func hashDiff(diff string) string {
	h := sha256.New()
	h.Write([]byte(diff))
	return hex.EncodeToString(h.Sum(nil))
}

func checkElasticCache(diffHash string) (string, bool) {
	resp, err := http.Get(fmt.Sprintf("http://127.0.0.1:9200/flume_meta_critic_cache/_doc/%s", diffHash))
	if err != nil || resp.StatusCode != 200 {
		return "", false
	}
	defer resp.Body.Close()
	var result map[string]interface{}
	json.NewDecoder(resp.Body).Decode(&result)

	if source, ok := result["_source"].(map[string]interface{}); ok {
		if critique, ok := source["critique"].(string); ok {
			return critique, true
		}
	}
	return "", false
}

func saveElasticCache(diffHash, critique string) {
	body := map[string]string{"critique": critique}
	jsonData, _ := json.Marshal(body)
	req, _ := http.NewRequest("PUT", fmt.Sprintf("http://127.0.0.1:9200/flume_meta_critic_cache/_doc/%s", diffHash), bytes.NewBuffer(jsonData))
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{}
	client.Do(req)
}

func extractModifiedFunctions(diff string) []string {
	var funcs []string
	re := regexp.MustCompile(`^\+.*(?:def|func) ([a-zA-Z0-9_]+)`)
	matches := re.FindAllStringSubmatch(diff, -1)
	for _, match := range matches {
		if len(match) > 1 {
			funcs = append(funcs, match[1])
		}
	}
	return funcs
}

func getBlastRadius(funcs []string) string {
	if len(funcs) == 0 {
		return ""
	}
	var blast []string
	for _, f := range funcs {
		cmd := exec.Command("elastro", "doc", "search", "fremen_codebase_rag", "--match", "functions_called="+f)
		var out bytes.Buffer
		cmd.Stdout = &out
		if err := cmd.Run(); err == nil && out.String() != "" && !strings.Contains(out.String(), "0 hits") {
			blast = append(blast, fmt.Sprintf("- Function '%s' triggers callers in:\n  %s", f, strings.TrimSpace(out.String())))
		}
	}
	if len(blast) > 0 {
		return "\n\n### Elastro Blast Radius Context (Files dependent on your modifications):\n" + strings.Join(blast, "\n")
	}
	return ""
}

func analyzeWithLLM(diff, apiKey string) string {
	modifiedFuncs := extractModifiedFunctions(diff)
	elastroCtx := getBlastRadius(modifiedFuncs)

	prompt := fmt.Sprintf(`You are an Elite Agentic Code Reviewer acting as a "Meta-Critic". Evaluate this Git pull request diff against the following standards:
- The Netflix Standard: No silent exception suppression (no bare 'pass' blocks). Explicitly log exceptions.
- The OWASP Standard: Assume all inputs are malicious. Sanitize data.
- The Google Standard: Optimize for readability and strict formatting.

Pull Request Diff:
`+"```diff\n%s\n```%s"+`

If you find ANY violations, provide a concise critique requesting changes. 
If the code is flawless, respond exactly with "APPROVED".
Be highly technical and succinct.`, diff, elastroCtx)

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
