package commands

import (
	"bufio"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/spf13/cobra"
)

// ─── OpenAI Codex OAuth constants ────────────────────────────────────────────

const (
	codexAuthURL     = "https://auth.openai.com/oauth/authorize"
	codexTokenURL    = "https://auth.openai.com/oauth/token"
	// Public Codex client supports connector scopes only; requesting model scopes
	// returns invalid_scope during authorize redirect.
	codexScopes      = "openid profile email offline_access api.connectors.read api.connectors.invoke"
	codexRedirectURI = "http://localhost:1455/auth/callback"
	// Public Codex CLI OAuth app client. Used as a default for callback copy/paste flow.
	codexDefaultClientID = "app_EMoamEEZ73f0CkXaXp7hrann"
)

// codexModelCatalog is the built-in list of Codex-compatible models shown after login.
// Live models fetched from /v1/models are prepended automatically.
var codexModelCatalog = []struct {
	id, display, hint string
}{
	{"codex-mini-latest", "Codex Mini (Latest)", "fast, optimised for code — recommended for Flume agents"},
	{"gpt-5.4", "GPT-5.4", "latest flagship coding model"},
	{"gpt-5.4-mini", "GPT-5.4 Mini", "faster lower-cost GPT-5.4 variant"},
	{"gpt-5.3-codex", "GPT-5.3 Codex", "stable codex-focused GPT-5.3 line"},
	{"gpt-5.3-codex-spark", "GPT-5.3 Codex Spark", "fast spark variant of GPT-5.3 Codex"},
	{"gpt-5.3", "GPT-5.3", "legacy alias used by some codex deployments"},
	{"gpt-4.1", "GPT-4.1", "most capable flagship"},
	{"gpt-4.1-mini", "GPT-4.1 Mini", "balanced speed and capability"},
	{"gpt-4.1-nano", "GPT-4.1 Nano", "fastest, most lightweight"},
	{"o4-mini", "o4-mini", "reasoning model, great for complex multi-step tasks"},
	{"o3", "o3", "advanced reasoning"},
	{"o3-mini", "o3-mini", "lighter reasoning model"},
	{"gpt-4o", "GPT-4o", "legacy multimodal flagship"},
	{"gpt-4o-mini", "GPT-4o Mini", "legacy mini model"},
	{"gpt-4-turbo", "GPT-4 Turbo", "legacy turbo"},
}

// ─── CLI flags ────────────────────────────────────────────────────────────────

var (
	flagClientID    string
	flagRedirectURI string
	lastCodexResult codexLoginResult
)

type codexLoginResult struct {
	AccessToken string
	Model       string
}

// ─── Command tree ─────────────────────────────────────────────────────────────

var CodexOAuthCmd = &cobra.Command{
	Use:   "codex-oauth",
	Short: "Authenticate Flume with OpenAI Codex via OAuth",
	Long: `Log in to OpenAI Codex using the browser-based OAuth PKCE flow.

Flume prints an authorization URL → you open it in your browser → log in →
copy the redirect URL → paste it back here. Flume exchanges the code for
tokens and lets you pick the Codex model to use for all agent tasks.`,
}

var codexLoginCmd = &cobra.Command{
	Use:   "login",
	Short: "Start the browser OAuth flow and save credentials to Flume",
	RunE:  runCodexLogin,
}

var codexStatusCmd = &cobra.Command{
	Use:   "status",
	Short: "Show current Codex OAuth token status",
	RunE:  runCodexStatus,
}

var codexRefreshCmd = &cobra.Command{
	Use:   "refresh",
	Short: "Refresh the Codex access token using the stored refresh token",
	RunE:  runCodexRefresh,
}

func init() {
	codexLoginCmd.Flags().StringVar(&flagClientID, "client-id", codexDefaultClientID,
		"OpenAI OAuth client ID (from https://platform.openai.com/settings/organization/apps)")
	codexLoginCmd.Flags().StringVar(&flagRedirectURI, "redirect-uri", codexRedirectURI,
		"OAuth redirect URI registered for your app (must match exactly)")

	CodexOAuthCmd.AddCommand(codexLoginCmd)
	CodexOAuthCmd.AddCommand(codexStatusCmd)
	CodexOAuthCmd.AddCommand(codexRefreshCmd)
}

// ─── Login ────────────────────────────────────────────────────────────────────

func runCodexLogin(cmd *cobra.Command, args []string) error {
	reader := bufio.NewReader(os.Stdin)

	// 1. Resolve client ID
	clientID := flagClientID
	if clientID == "" {
		clientID = os.Getenv("OPENAI_OAUTH_CLIENT_ID")
	}
	if clientID == "" {
		clientID = codexDefaultClientID
	}

	redirectURI := flagRedirectURI
	if redirectURI == "" {
		redirectURI = codexRedirectURI
	}

	// 2. Generate PKCE
	verifier, err := generateVerifier()
	if err != nil {
		return fmt.Errorf("PKCE generation failed: %w", err)
	}
	challenge := pkceChallenge(verifier)
	state, err := randomHex(16)
	if err != nil {
		return fmt.Errorf("state generation failed: %w", err)
	}

	// 3. Build authorization URL
	q := url.Values{
		"response_type":         {"code"},
		"client_id":             {clientID},
		"redirect_uri":          {redirectURI},
		"scope":                 {codexScopes},
		"state":                 {state},
		"code_challenge":        {challenge},
		"code_challenge_method": {"S256"},
		// Match native Codex CLI OAuth authorize params.
		"id_token_add_organizations": {"true"},
		"codex_cli_simplified_flow":  {"true"},
		"originator":                 {"codex_cli_rs"},
	}
	authURL := codexAuthURL + "?" + q.Encode()

	// 4. Display instructions
	fmt.Println()
	fmt.Println(ui.NeonGreen("  OPENAI CODEX OAUTH LOGIN  "))
	fmt.Println()
	fmt.Println(ui.WarningGold("Step 1") + "  Open this URL in your browser:")
	fmt.Println()
	fmt.Println("  " + ui.SuccessBlue(authURL))
	fmt.Println()
	fmt.Println(ui.WarningGold("Step 2") + "  Log in with your OpenAI account and authorize Flume.")
	fmt.Println()
	fmt.Println(ui.WarningGold("Step 3") + "  After authorization, your browser will redirect to a callback URL.")
	fmt.Println("         Copy the full callback URL from your browser address bar.")
	fmt.Println("         (It should start with " + ui.Dim(redirectURI[:minInt(40, len(redirectURI))]) + "…)")
	fmt.Println("         and paste it here:")
	fmt.Println()
	fmt.Print(ui.WarningGold("Redirect URL ▶ "))

	redirected, err := reader.ReadString('\n')
	if err != nil {
		return fmt.Errorf("failed to read redirect URL: %w", err)
	}
	redirected = strings.TrimSpace(redirected)
	if redirected == "" {
		return fmt.Errorf("no redirect URL provided — aborting")
	}

	// 5. Extract & validate code
	if oauthErr, _ := extractQueryParam(redirected, "error"); oauthErr != "" {
		oauthDesc, _ := extractQueryParam(redirected, "error_description")
		if oauthDesc == "" {
			oauthDesc = "authorization server returned an OAuth error"
		}
		return fmt.Errorf("oauth authorize failed: %s (%s)", oauthErr, oauthDesc)
	}
	code, err := extractQueryParam(redirected, "code")
	if err != nil || code == "" {
		return fmt.Errorf("could not find 'code' in redirect URL.\n" +
			"Make sure you copied the entire browser URL after authorization.\n" +
			"URL received: " + redirected)
	}
	if returnedState, _ := extractQueryParam(redirected, "state"); returnedState != "" && returnedState != state {
		return fmt.Errorf("OAuth state mismatch — possible CSRF. Please run login again")
	}

	// 6. Exchange code → tokens
	fmt.Println()
	fmt.Println(ui.Dim("Exchanging authorization code for tokens…"))

	tokens, err := exchangeCode(clientID, code, redirectURI, verifier)
	if err != nil {
		return fmt.Errorf("token exchange failed: %w", err)
	}

	accessToken := tokens["access_token"]
	refreshToken := tokens["refresh_token"]
	if accessToken == "" {
		return fmt.Errorf("OpenAI did not return an access_token")
	}

	expiresIn := int64(0)
	if v, ok := tokens["expires_in"]; ok {
		fmt.Sscanf(v, "%d", &expiresIn)
	}
	expiresMs := time.Now().UnixMilli() + expiresIn*1000

	// 7. Fetch live models for selection
	fmt.Println(ui.Dim("Fetching available models from OpenAI…"))
	liveModels := fetchLiveModels(accessToken)

	// 8. Save OAuth state to Flume dashboard
	stateDoc := map[string]any{
		"client_id":              clientID,
		"access":                 accessToken,
		"refresh":                refreshToken,
		"expires":                expiresMs,
		"oauth_scopes_requested": codexScopes,
	}
	flumeClient := ui.NewFlumeClient()
	if _, err := flumeClient.Post("/api/settings/llm/oauth/save", stateDoc); err != nil {
		// During `flume start`, dashboard may not be up yet. Continue with local result.
		fmt.Println(ui.WarningGold("Dashboard not reachable yet; continuing with local OAuth session."))
	}

	fmt.Println()
	fmt.Println(ui.SuccessBlue("✓ OAuth tokens saved successfully."))
	fmt.Println()

	// 9. Let user pick a model
	model, err := pickCodexModel(reader, liveModels)
	if err != nil {
		return fmt.Errorf("model selection failed: %w", err)
	}

	// 10. Save provider + model
	if _, err := flumeClient.Put("/api/settings/llm", map[string]any{
		"provider": "openai",
		"model":    model,
		"authMode": "oauth",
	}); err != nil {
		fmt.Println(ui.WarningGold("Could not persist model to dashboard yet; it will be set for this start session."))
	}
	lastCodexResult = codexLoginResult{AccessToken: accessToken, Model: model}

	fmt.Println()
	fmt.Println(ui.SuccessBlue("✓ Provider set to OpenAI Codex"))
	fmt.Printf("  Model: %s\n", ui.NeonGreen(model))
	fmt.Println()
	fmt.Println("Flume agents will now use OpenAI Codex for all tasks.")
	fmt.Println("Run " + ui.NeonGreen("flume codex-oauth status") + " to verify, or " +
		ui.NeonGreen("flume codex-oauth refresh") + " when the token expires.")
	return nil
}

func getLastCodexLoginResult() codexLoginResult {
	return lastCodexResult
}

func runNativeCodexLoginAndImport(reader *bufio.Reader) error {
	fmt.Println()
	fmt.Println(ui.NeonGreen("  OPENAI CODEX OAUTH LOGIN  "))
	fmt.Println()
	fmt.Println(ui.Dim("No OAuth client ID configured; using native Codex login flow."))
	if useDeviceAuth() {
		fmt.Println(ui.Dim("Using device-code authentication."))
		fmt.Println(ui.Dim("Open the printed URL and enter the one-time code in your browser."))
	} else {
		fmt.Println(ui.Dim("This will take you directly to OpenAI authentication."))
		fmt.Println(ui.Dim("After login, copy the callback URL and paste it back into the CLI if prompted."))
	}
	fmt.Println()

	loginCmd, err := nativeCodexLoginCommand()
	if err != nil {
		return err
	}
	loginCmd.Stdin = os.Stdin
	loginCmd.Stdout = os.Stdout
	loginCmd.Stderr = os.Stderr
	if err := loginCmd.Run(); err != nil {
		return fmt.Errorf("native codex login failed: %w", err)
	}

	state, err := loadCodexAuthStateFromDisk()
	if err != nil {
		return fmt.Errorf("login succeeded but could not import credentials into Flume: %w", err)
	}

	flumeClient := ui.NewFlumeClient()
	if _, err := flumeClient.Post("/api/settings/llm/oauth/save", state); err != nil {
		return fmt.Errorf("could not save OAuth state to Flume dashboard: %w", err)
	}

	accessToken := strFromMap(state, "access")
	liveModels := fetchLiveModels(accessToken)
	model, err := pickCodexModel(reader, liveModels)
	if err != nil {
		return fmt.Errorf("model selection failed: %w", err)
	}
	if _, err := flumeClient.Put("/api/settings/llm", map[string]any{
		"provider": "openai",
		"model":    model,
		"authMode": "oauth",
	}); err != nil {
		return fmt.Errorf("failed to save LLM provider settings: %w", err)
	}
	fmt.Println()
	fmt.Println(ui.SuccessBlue("✓ Native Codex auth imported into Flume"))
	fmt.Printf("  Model: %s\n", ui.NeonGreen(model))
	return nil
}

func nativeCodexLoginCommand() (*exec.Cmd, error) {
	args := []string{"login"}
	if useDeviceAuth() {
		args = []string{"login", "--device-auth"}
	}
	if path, err := exec.LookPath("codex"); err == nil {
		return exec.Command(path, args...), nil
	}
	if path, err := exec.LookPath("npx"); err == nil {
		return exec.Command(path, append([]string{"--yes", "@openai/codex"}, args...)...), nil
	}
	return nil, fmt.Errorf("neither 'codex' nor 'npx' is available on PATH")
}

func useDeviceAuth() bool {
	// Explicit selector: callback (default) or device.
	mode := strings.ToLower(strings.TrimSpace(os.Getenv("FLUME_CODEX_AUTH_MODE")))
	switch mode {
	case "", "callback":
		return false
	case "device", "device-auth", "device_code":
		return true
	}
	// Backward-compatible boolean override.
	if v := strings.ToLower(strings.TrimSpace(os.Getenv("FLUME_CODEX_HEADLESS"))); v != "" {
		return v == "1" || v == "true" || v == "yes" || v == "on"
	}
	return false
}

func loadCodexAuthStateFromDisk() (map[string]any, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, fmt.Errorf("resolve home dir: %w", err)
	}
	authPath := filepath.Join(home, ".codex", "auth.json")
	raw, err := os.ReadFile(authPath)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", authPath, err)
	}

	var payload any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, fmt.Errorf("parse %s: %w", authPath, err)
	}

	clientID := firstNonEmpty(
		findStringAny(payload, "client_id"),
		findStringAny(payload, "clientId"),
	)
	access := firstNonEmpty(
		findStringAny(payload, "access"),
		findStringAny(payload, "access_token"),
		findStringAny(payload, "accessToken"),
	)
	refresh := firstNonEmpty(
		findStringAny(payload, "refresh"),
		findStringAny(payload, "refresh_token"),
		findStringAny(payload, "refreshToken"),
	)
	expiresRaw := firstNonEmpty(
		findStringAny(payload, "expires"),
		findStringAny(payload, "expires_at"),
		findStringAny(payload, "expiresAt"),
	)
	if clientID == "" || access == "" || refresh == "" {
		return nil, fmt.Errorf("missing required fields in %s (client_id/access/refresh)", authPath)
	}

	expiresMs := int64(0)
	if expiresRaw != "" {
		if n, err := strconv.ParseInt(expiresRaw, 10, 64); err == nil {
			// Heuristic: treat small values as seconds, large as ms.
			if n > 0 && n < 1_000_000_000_000 {
				n *= 1000
			}
			expiresMs = n
		}
	}

	state := map[string]any{
		"client_id":              clientID,
		"access":                 access,
		"refresh":                refresh,
		"oauth_scopes_requested": codexScopes,
	}
	if expiresMs > 0 {
		state["expires"] = expiresMs
	}
	return state, nil
}

func findStringAny(v any, key string) string {
	switch t := v.(type) {
	case map[string]any:
		for k, val := range t {
			if strings.EqualFold(k, key) {
				if s, ok := val.(string); ok {
					return strings.TrimSpace(s)
				}
			}
		}
		for _, val := range t {
			if s := findStringAny(val, key); s != "" {
				return s
			}
		}
	case []any:
		for _, val := range t {
			if s := findStringAny(val, key); s != "" {
				return s
			}
		}
	}
	return ""
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

func strFromMap(m map[string]any, key string) string {
	if m == nil {
		return ""
	}
	if v, ok := m[key]; ok {
		if s, ok := v.(string); ok {
			return strings.TrimSpace(s)
		}
	}
	return ""
}

// ─── Status ───────────────────────────────────────────────────────────────────

func runCodexStatus(cmd *cobra.Command, args []string) error {
	client := ui.NewFlumeClient()
	data, err := client.Get("/api/settings/llm")
	if err != nil {
		return fmt.Errorf("cannot reach Flume dashboard: %w", err)
	}

	provider, _ := data["provider"].(string)
	model, _ := data["model"].(string)
	if settings, ok := data["settings"].(map[string]any); ok && settings != nil {
		if p, ok := settings["provider"].(string); ok && strings.TrimSpace(p) != "" {
			provider = p
		}
		if m, ok := settings["model"].(string); ok && strings.TrimSpace(m) != "" {
			model = m
		}
	}

	fmt.Println()
	fmt.Println(ui.NeonGreen("  CODEX OAUTH STATUS  "))
	fmt.Println()
	fmt.Printf("  Active provider: %s\n", ui.SuccessBlue(provider))
	fmt.Printf("  Active model:    %s\n", ui.SuccessBlue(model))
	fmt.Println()

	oauthStatus, _ := data["oauthStatus"].(map[string]any)
	if oauthStatus == nil {
		fmt.Println(ui.WarningGold("  No OAuth state found. Run: flume codex-oauth login"))
		return nil
	}

	configured := false
	switch v := oauthStatus["configured"].(type) {
	case bool:
		configured = v
	case string:
		configured = strings.TrimSpace(v) != ""
	}
	if !configured {
		msg, _ := oauthStatus["message"].(string)
		if strings.TrimSpace(msg) == "" {
			msg = "OAuth state not configured"
		}
		fmt.Println(ui.WarningGold("  OAuth not configured: ") + msg)
		fmt.Println("  Run: " + ui.NeonGreen("flume codex-oauth login"))
		return nil
	}

	hasAccess, _ := oauthStatus["hasAccessToken"].(bool)
	expSec, _ := oauthStatus["expiresInSeconds"].(float64)
	clientID, _ := oauthStatus["clientId"].(string)
	scopeStatus, _ := oauthStatus["oauthScopeStatus"].(string)
	hasWrite, _ := oauthStatus["hasApiResponsesWrite"].(bool)
	hasModel, _ := oauthStatus["hasModelRequestScope"].(bool)

	tokenIcon := ui.SuccessBlue("✓")
	if !hasAccess {
		tokenIcon = ui.WarningGold("!")
	}

	fmt.Printf("  %s Access token    : %s\n", tokenIcon, yesNo(hasAccess))
	fmt.Printf("     Client ID       : %s\n", clientID)
	fmt.Printf("     Expires in      : %s\n", humanSeconds(int64(expSec)))
	fmt.Printf("     Scope status    : %s\n", scopeStatus)
	fmt.Printf("     model.request   : %s\n", yesNo(hasModel))
	fmt.Printf("     api.responses   : %s\n", yesNo(hasWrite))

	if scopes, ok := oauthStatus["accessTokenScopes"].([]any); ok && len(scopes) > 0 {
		ss := make([]string, 0, len(scopes))
		for _, s := range scopes {
			if str, ok := s.(string); ok {
				ss = append(ss, str)
			}
		}
		fmt.Printf("     Scopes          : %s\n", strings.Join(ss, " "))
	}
	fmt.Println()

	if !hasAccess || expSec < 300 {
		fmt.Println(ui.WarningGold("  Token is expired or expiring soon.") +
			"  Run: " + ui.NeonGreen("flume codex-oauth refresh"))
	}
	return nil
}

// ─── Refresh ──────────────────────────────────────────────────────────────────

func runCodexRefresh(cmd *cobra.Command, args []string) error {
	client := ui.NewFlumeClient()
	data, err := client.Post("/api/settings/llm/oauth/refresh", nil)
	if err != nil {
		return fmt.Errorf("refresh request failed: %w", err)
	}
	if errMsg, ok := data["error"].(string); ok && errMsg != "" {
		return fmt.Errorf("refresh failed: %s", errMsg)
	}
	fmt.Println(ui.SuccessBlue("✓ Codex OAuth token refreshed successfully."))
	return nil
}

// ─── PKCE helpers ─────────────────────────────────────────────────────────────

func generateVerifier() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

func pkceChallenge(verifier string) string {
	h := sha256.Sum256([]byte(verifier))
	return base64.RawURLEncoding.EncodeToString(h[:])
}

func randomHex(n int) (string, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

// ─── Token exchange ───────────────────────────────────────────────────────────

func exchangeCode(clientID, code, redirectURI, verifier string) (map[string]string, error) {
	form := url.Values{
		"grant_type":    {"authorization_code"},
		"client_id":     {clientID},
		"code":          {code},
		"redirect_uri":  {redirectURI},
		"code_verifier": {verifier},
	}
	resp, err := http.PostForm(codexTokenURL, form)
	if err != nil {
		return nil, fmt.Errorf("HTTP error contacting %s: %w", codexTokenURL, err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(io.LimitReader(resp.Body, 64*1024))

	var raw map[string]any
	if err := json.Unmarshal(body, &raw); err != nil {
		snip := string(body)
		if len(snip) > 300 {
			snip = snip[:300]
		}
		return nil, fmt.Errorf("invalid JSON from token endpoint: %s", snip)
	}
	if resp.StatusCode != http.StatusOK {
		errCode, _ := raw["error"].(string)
		errDesc, _ := raw["error_description"].(string)
		return nil, fmt.Errorf("HTTP %d: %s — %s", resp.StatusCode, errCode, errDesc)
	}

	out := map[string]string{}
	for _, k := range []string{"access_token", "refresh_token", "token_type", "scope"} {
		if v, ok := raw[k].(string); ok {
			out[k] = v
		}
	}
	// expires_in is a number; stringify for transport
	if v, ok := raw["expires_in"].(float64); ok {
		out["expires_in"] = fmt.Sprintf("%d", int64(v))
	}
	return out, nil
}

// ─── Live model discovery ─────────────────────────────────────────────────────

func fetchLiveModels(accessToken string) []string {
	req, err := http.NewRequest("GET", "https://api.openai.com/v1/models", nil)
	if err != nil {
		return nil
	}
	req.Header.Set("Authorization", "Bearer "+accessToken)
	client := &http.Client{Timeout: 8 * time.Second}
	resp, err := client.Do(req)
	if err != nil || resp.StatusCode != http.StatusOK {
		return nil
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(io.LimitReader(resp.Body, 512*1024))
	var payload struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		return nil
	}

	ids := make([]string, 0, len(payload.Data))
	for _, m := range payload.Data {
		if m.ID != "" {
			ids = append(ids, m.ID)
		}
	}
	sort.Strings(ids)
	return ids
}

// ─── Model picker ─────────────────────────────────────────────────────────────

func pickCodexModel(reader *bufio.Reader, liveModels []string) (string, error) {
	// Build de-duplicated choice list: live-discovered first, then catalog
	seen := map[string]bool{}
	type choice struct{ id, display, hint string }
	var choices []choice

	// Live models not in the catalog go at the top (they're freshly available)
	for _, id := range liveModels {
		if !seen[id] {
			seen[id] = true
			// Check if it's in our catalog for a nice display name
			display, hint := id, "from OpenAI /v1/models"
			for _, c := range codexModelCatalog {
				if c.id == id {
					display, hint = c.display, c.hint
					break
				}
			}
			choices = append(choices, choice{id, display, hint})
		}
	}
	// Then add catalog entries not already listed
	for _, c := range codexModelCatalog {
		if !seen[c.id] {
			seen[c.id] = true
			choices = append(choices, choice{c.id, c.display, c.hint})
		}
	}

	fmt.Println(ui.NeonGreen("Select model for Flume agents:"))
	fmt.Println()
	for i, c := range choices {
		hint := ""
		if c.hint != "" {
			hint = ui.Dim("  — " + c.hint)
		}
		fmt.Printf("  %2d.  %-32s %s%s\n",
			i+1,
			ui.SuccessBlue(c.id),
			c.display,
			hint,
		)
	}
	fmt.Println()
	fmt.Print(ui.WarningGold(fmt.Sprintf("Enter number (1–%d) or type a model ID: ", len(choices))))

	line, err := reader.ReadString('\n')
	if err != nil {
		return "", err
	}
	line = strings.TrimSpace(line)

	if line == "" && len(choices) > 0 {
		// Default: first item (codex-mini-latest or first live model)
		return choices[0].id, nil
	}
	for i, c := range choices {
		if line == fmt.Sprintf("%d", i+1) {
			return c.id, nil
		}
	}
	// Treat anything else as a literal model ID
	if line != "" {
		return line, nil
	}
	return "codex-mini-latest", nil
}

// ─── URL helpers ──────────────────────────────────────────────────────────────

func extractQueryParam(rawURL, key string) (string, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return "", err
	}
	return u.Query().Get(key), nil
}

// ─── Display helpers ──────────────────────────────────────────────────────────

func yesNo(v bool) string {
	if v {
		return ui.SuccessBlue("yes")
	}
	return ui.WarningGold("no")
}

func humanSeconds(sec int64) string {
	if sec <= 0 {
		return ui.WarningGold("expired")
	}
	if sec < 60 {
		return fmt.Sprintf("%ds", sec)
	}
	if sec < 3600 {
		return fmt.Sprintf("%dm %ds", sec/60, sec%60)
	}
	return fmt.Sprintf("%dh %dm", sec/3600, (sec%3600)/60)
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}
