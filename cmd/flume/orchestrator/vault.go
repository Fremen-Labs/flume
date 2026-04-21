package orchestrator

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/charmbracelet/log"
)

// VaultKeys represents the data structured natively during initialization securely.
type VaultKeys struct {
	KeysB64   []string `json:"keys_base64"`
	RootToken string   `json:"root_token"`
}

// GenerateESAPIKey securely generates a 32-byte hex entropy key.
func GenerateESAPIKey() (string, error) {
	bytes := make([]byte, 32)
	if _, err := rand.Read(bytes); err != nil {
		return "", err
	}
	return hex.EncodeToString(bytes), nil
}

// AwaitOpenBao gracefully awaits native OpenBao cluster locks indefinitely.
func AwaitOpenBao(ctx context.Context, vaultURL string) error {
	log.Info("Awaiting OpenBao KMS Cluster Generation Locks...", "url", vaultURL)

	for i := 0; i < 40; i++ {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}

		resp, err := doVaultRequest(ctx, "GET", fmt.Sprintf("%s/v1/sys/health", vaultURL), "", nil)
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == 200 || resp.StatusCode == 429 || resp.StatusCode == 472 || resp.StatusCode == 473 || resp.StatusCode == 501 || resp.StatusCode == 503 {
				log.Info("OpenBao Boot Sequenced Successfully.")
				return nil
			}
		}
		
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
	return fmt.Errorf("openbao initialization timeout")
}

// InitializeAndUnseal loads or creates the cluster keys and unseals it natively.
func InitializeAndUnseal(ctx context.Context, vaultURL string) (string, error) {
	resp, err := doVaultRequest(ctx, "GET", fmt.Sprintf("%s/v1/sys/init", vaultURL), "", nil)
	if err != nil {
		return "", fmt.Errorf("failed to check vault init status: %w", err)
	}
	defer resp.Body.Close()

	var initStatus struct {
		Initialized bool `json:"initialized"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&initStatus); err != nil {
		return "", fmt.Errorf("failed to parse init status: %w", err)
	}

	var keys VaultKeys

	if !initStatus.Initialized {
		log.Info("First true boot detected: Initializing OpenBao cluster securely.")
		initPayload := map[string]interface{}{
			"secret_shares":    1,
			"secret_threshold": 1,
		}

		initResp, err := doVaultRequest(ctx, "POST", fmt.Sprintf("%s/v1/sys/init", vaultURL), "", initPayload)
		if err != nil || initResp.StatusCode != 200 {
			if initResp != nil {
				initResp.Body.Close()
			}
			return "", fmt.Errorf("vault init failed: %w", err)
		}
		defer initResp.Body.Close()

		if err := json.NewDecoder(initResp.Body).Decode(&keys); err != nil {
			return "", fmt.Errorf("failed to extract generated vault keys natively: %w", err)
		}

		os.MkdirAll(filepath.Join(os.Getenv("HOME"), ".flume"), 0700)
		
		log.Warn("==================================================================")
		log.Warn("🔐 OPENBAO NATIVE KMS DEPLOYED SUCCESSFULLY 🔐")
		log.Warn("Please save these credentials to a secure password manager NOW.")
		log.Warn(fmt.Sprintf("Root Token : %s", keys.RootToken))
		if len(keys.KeysB64) > 0 {
			log.Warn(fmt.Sprintf("Unseal Key : %s", keys.KeysB64[0]))
		}
		log.Warn("==================================================================")
	} else {
		log.Info("Persistent OpenBao cluster detected.")
		// Vault is already initialized. If it is also already unsealed we need
		// to mint a fresh root token via the generate-root workflow, because
		// we have no persisted token from a previous run (orphaned volume).
		// We check health first: 200 = unsealed, 503 = sealed.
		healthResp, hErr := doVaultRequest(ctx, "GET", fmt.Sprintf("%s/v1/sys/health", vaultURL), "", nil)
		if hErr != nil {
			log.Warn("Vault health check failed during orphan recovery check — cannot determine sealed state", "error", hErr)
		} else {
			healthResp.Body.Close()
			if healthResp.StatusCode == 200 {
				// Already unsealed — generate a fresh root token.
				log.Warn("Orphaned Vault detected (unsealed, no root token). Recovering via generate-root workflow.")
				recoveredToken, rErr := GenerateRootToken(ctx, vaultURL)
				if rErr != nil {
					log.Error("generate-root recovery failed. Run: flume destroy --purge && flume start", "error", rErr)
					return "", fmt.Errorf("vault root-token recovery failed: %w", rErr)
				}
				keys.RootToken = recoveredToken
				log.Info("Successfully recovered root token via generate-root workflow.")
				return keys.RootToken, nil
			}
		}
	}

	// Determine sealed state natively
	respHealth, err := doVaultRequest(ctx, "GET", fmt.Sprintf("%s/v1/sys/health", vaultURL), "", nil)
	if err != nil {
		return "", fmt.Errorf("health check failed: %w", err)
	}
	defer respHealth.Body.Close()
	
	if respHealth.StatusCode == 503 { // Sealed natively
		log.Warn("Your OpenBao cluster is natively sealed.")
		if len(keys.KeysB64) == 0 {
			unsealKey := os.Getenv("FLUME_BAO_UNSEAL_KEY")
			rootToken := os.Getenv("FLUME_BAO_ROOT_TOKEN")

			if unsealKey == "" {
				fmt.Print("Please enter your OpenBao Unseal Key: ")
				fmt.Scanln(&unsealKey)
			}
			keys.KeysB64 = append(keys.KeysB64, strings.TrimSpace(unsealKey))
			
			if rootToken == "" {
				fmt.Print("Please enter your OpenBao Root Token (or press Enter for default dev token): ")
				fmt.Scanln(&rootToken)
			}
			keys.RootToken = strings.TrimSpace(rootToken)
			
			if keys.RootToken == "" {
				keys.RootToken = "flume-dev-token" // Fallback to dev map if they bypassed it initially
			}
		}

		unsealPayload := map[string]interface{}{
			"key": keys.KeysB64[0],
		}
		unsealResp, err := doVaultRequest(ctx, "POST", fmt.Sprintf("%s/v1/sys/unseal", vaultURL), "", unsealPayload)
		if err != nil || unsealResp.StatusCode != 200 {
			if unsealResp != nil {
				unsealResp.Body.Close()
			}
			return "", fmt.Errorf("failed to submit unseal KMS natively: %w", err)
		}
		unsealResp.Body.Close()
		log.Info("OpenBao KMS Unsealed Successfully.")
	} else {
		log.Info("OpenBao KMS already unsealed. Continuing...")
	}

	return keys.RootToken, nil
}

// doVaultRequest is a core helper for submitting HTTP sequences natively towards Vault.
func doVaultRequest(ctx context.Context, method, url, token string, body interface{}) (*http.Response, error) {
	client := &http.Client{Timeout: 10 * time.Second}
	var reader io.Reader
	if body != nil {
		bodyBytes, _ := json.Marshal(body)
		reader = bytes.NewReader(bodyBytes)
	}
	req, err := http.NewRequestWithContext(ctx, method, url, reader)
	if err != nil {
		return nil, err
	}
	if token != "" {
		req.Header.Set("X-Vault-Token", token)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	return client.Do(req)
}

// GenerateRootToken mints a fresh root token via the Vault sys/generate-root workflow.
// Required when the Vault is already initialized + unsealed but the original root_token
// is lost (orphaned persistent volume scenario). Uses a one-time OTP pad for encoding.
func GenerateRootToken(ctx context.Context, vaultURL string) (string, error) {
	// Generate a 16-byte OTP and base64-encode it for the Vault API.
	otpRaw := make([]byte, 16)
	if _, err := rand.Read(otpRaw); err != nil {
		return "", fmt.Errorf("failed to generate OTP for root token: %w", err)
	}
	otpB64 := base64.StdEncoding.EncodeToString(otpRaw)

	// Step 1 — start the generate-root attempt
	startResp, err := doVaultRequest(ctx, "PUT", fmt.Sprintf("%s/v1/sys/generate-root/attempt", vaultURL), "",
		map[string]interface{}{"otp": otpB64})
	if err != nil {
		return "", fmt.Errorf("generate-root/attempt request failed: %w", err)
	}
	defer startResp.Body.Close()
	if startResp.StatusCode != 200 && startResp.StatusCode != 204 {
		body, readErr := io.ReadAll(startResp.Body)
		if readErr != nil {
			return "", fmt.Errorf("generate-root/attempt failed (%d); could not read response body: %w", startResp.StatusCode, readErr)
		}
		return "", fmt.Errorf("generate-root/attempt failed (%d): %s", startResp.StatusCode, string(body))
	}
	var attemptData struct {
		Nonce string `json:"nonce"`
	}
	if err := json.NewDecoder(startResp.Body).Decode(&attemptData); err != nil {
		return "", fmt.Errorf("failed to decode generate-root attempt response: %w", err)
	}

	// Step 2 — provide the unseal key (empty = already unsealed, 1-of-1 share)
	updateResp, err := doVaultRequest(ctx, "PUT", fmt.Sprintf("%s/v1/sys/generate-root/update", vaultURL), "",
		map[string]interface{}{"key": "", "nonce": attemptData.Nonce})
	if err != nil {
		return "", fmt.Errorf("generate-root/update request failed: %w", err)
	}
	defer updateResp.Body.Close()
	if updateResp.StatusCode != 200 && updateResp.StatusCode != 204 {
		body, readErr := io.ReadAll(updateResp.Body)
		if readErr != nil {
			return "", fmt.Errorf("generate-root/update failed (%d); could not read response body: %w", updateResp.StatusCode, readErr)
		}
		return "", fmt.Errorf("generate-root/update failed (%d): %s", updateResp.StatusCode, string(body))
	}
	var resultData struct {
		Complete         bool   `json:"complete"`
		EncodedRootToken string `json:"encoded_root_token"`
	}
	if err := json.NewDecoder(updateResp.Body).Decode(&resultData); err != nil {
		return "", fmt.Errorf("failed to decode generate-root result: %w", err)
	}
	if !resultData.Complete {
		return "", fmt.Errorf("generate-root did not complete — vault may require more unseal key shares")
	}

	// Step 3 — XOR-decode: token_bytes = base64.StdEncoding.DecodeString(encoded) XOR otp_raw
	decodedToken, err := base64.StdEncoding.DecodeString(resultData.EncodedRootToken)
	if err != nil {
		// Vault sometimes returns unpadded base64; try RawStdEncoding as a fallback.
		decodedToken, err = base64.RawStdEncoding.DecodeString(resultData.EncodedRootToken)
		if err != nil {
			return "", fmt.Errorf("failed to base64-decode root token: %w", err)
		}
	}
	tokenBytes := make([]byte, len(decodedToken))
	for i := range tokenBytes {
		if i < len(otpRaw) {
			tokenBytes[i] = decodedToken[i] ^ otpRaw[i%len(otpRaw)]
		} else {
			tokenBytes[i] = decodedToken[i]
		}
	}
	// Root token is a UTF-8 string; trim null bytes.
	rootToken := strings.TrimRight(string(tokenBytes), "\x00")
	if rootToken == "" {
		return "", fmt.Errorf("generate-root produced empty token — check vault logs")
	}
	return rootToken, nil
}


// ConfigureSecretsEngine structures the KeyVault KV topology dynamically natively.
func ConfigureSecretsEngine(ctx context.Context, vaultURL, rootToken string, envCfg EnvConfig) error {
	// 1. Enable KV v2 natively at "secret/"
	sysMountsResp, err := doVaultRequest(ctx, "GET", fmt.Sprintf("%s/v1/sys/mounts", vaultURL), rootToken, nil)
	if err != nil {
		return fmt.Errorf("failed to query mounts: %w", err)
	}
	defer sysMountsResp.Body.Close()

	var mountsData map[string]interface{}
	json.NewDecoder(sysMountsResp.Body).Decode(&mountsData)

	if _, exists := mountsData["secret/"]; !exists && mountsData["data"] != nil {
		// Try under data dynamically
		if dataMounts, ok := mountsData["data"].(map[string]interface{}); ok {
			if _, existsIn := dataMounts["secret/"]; !existsIn {
				mountConf := map[string]interface{}{
					"type": "kv",
					"options": map[string]interface{}{
						"version": "2",
					},
				}
				mountResp, mErr := doVaultRequest(ctx, "POST", fmt.Sprintf("%s/v1/sys/mounts/secret", vaultURL), rootToken, mountConf)
				if mErr != nil || (mountResp.StatusCode != 200 && mountResp.StatusCode != 204) {
					if mountResp != nil {
						mountResp.Body.Close()
					}
					return fmt.Errorf("failed to enable secrets native engine natively: %w", mErr)
				}
				mountResp.Body.Close()
				log.Info("Successfully enabled Vault secret engine at secret/ natively.")
			} else {
				log.Info("Secret engine 'secret/' already exists. Skipping creation.")
			}
		}
	} else if !exists {
		// API mapping differences across OpenBao versions dynamically handled
		mountConf := map[string]interface{}{
			"type": "kv",
			"options": map[string]interface{}{
				"version": "2",
			},
		}
		mountResp, mErr := doVaultRequest(ctx, "POST", fmt.Sprintf("%s/v1/sys/mounts/secret", vaultURL), rootToken, mountConf)
		if mErr != nil || (mountResp.StatusCode != 200 && mountResp.StatusCode != 204) {
			if mountResp != nil {
				mountResp.Body.Close()
			}
			return fmt.Errorf("failed to enable secrets native engine natively: %w", mErr)
		}
		mountResp.Body.Close()
		log.Info("Successfully enabled Vault secret engine at secret/ natively.")
	}

	// 2. Resolve KV payload
	esKey, _ := GenerateESAPIKey()

	// OpenBao holds ONLY actual secrets — LLM config (model/provider/baseUrl) is non-sensitive
	// and is written to ES flume-llm-config by SeedLLMConfig during startup.
	kvPayload := map[string]string{
		"ES_API_KEY": esKey,
	}

	// Explicit bindings properly stripped natively
	if envCfg.APIKey != "" {
		kvPayload["OPENAI_API_KEY"] = envCfg.APIKey
		kvPayload["LLM_API_KEY"] = envCfg.APIKey
	}
	if envCfg.GithubToken != "" {
		kvPayload["GH_TOKEN"] = envCfg.GithubToken
	}
	if envCfg.ADOToken != "" {
		kvPayload["ADO_TOKEN"] = envCfg.ADOToken
	}

	// Fetch existing configurations before injecting gracefully natively
	existResp, eErr := doVaultRequest(ctx, "GET", fmt.Sprintf("%s/v1/secret/data/flume/keys", vaultURL), rootToken, nil)
	if eErr == nil && existResp.StatusCode == 200 {
		var existMap map[string]interface{}
		json.NewDecoder(existResp.Body).Decode(&existMap)
		existResp.Body.Close()

		if dataOuter, ok := existMap["data"].(map[string]interface{}); ok {
			if dataInner, ok := dataOuter["data"].(map[string]interface{}); ok {
				combined := make(map[string]string)
				// Cast dynamically natively
				for k, v := range dataInner {
					if strV, ok2 := v.(string); ok2 {
						combined[k] = strV
					}
				}

				// Apply overrides: ES_API_KEY always overwrites; all other secrets
				// (ADO_TOKEN, GH_TOKEN, LLM_API_KEY) only written if explicitly provided.
				for k, v := range kvPayload {
					if k == "ES_API_KEY" {
						combined[k] = v // Always rotate the generated key
					} else {
						// Any other key is only present because the user explicitly provided it in this run.
						combined[k] = v
					}
				}
				kvPayload = combined
			}
		}
	} else if existResp != nil {
		existResp.Body.Close()
	}

	// Create or update secret
	writeConf := map[string]interface{}{
		"data": kvPayload,
	}
	writeResp, wErr := doVaultRequest(ctx, "POST", fmt.Sprintf("%s/v1/secret/data/flume/keys", vaultURL), rootToken, writeConf)
	if wErr != nil || (writeResp.StatusCode != 200 && writeResp.StatusCode != 204) {
		if writeResp != nil {
			body, _ := io.ReadAll(writeResp.Body)
			writeResp.Body.Close()
			return fmt.Errorf("failed to sink KV bindings into OpenBao natively (%v): %s", writeResp.StatusCode, string(body))
		}
		return fmt.Errorf("failed to sink KV bindings into OpenBao natively: %w", wErr)
	}
	writeResp.Body.Close()

	keysWritten := make([]string, 0, len(kvPayload))
	for k := range kvPayload {
		keysWritten = append(keysWritten, k)
	}
	log.Info("Injected Infrastructure Configuration + API keys seamlessly into OpenBao KV.", "keys", strings.Join(keysWritten, ", "))

	return nil
}

// ProvisionAppRole enables the AppRole engine seamlessly and retrieves the dynamically bound flume-worker Token secret.
func ProvisionAppRole(ctx context.Context, vaultURL, rootToken string) (string, error) {
	// Enable approle dynamically natively
	aResp, aErr := doVaultRequest(ctx, "GET", fmt.Sprintf("%s/v1/sys/auth", vaultURL), rootToken, nil)
	if aErr != nil {
		return "", fmt.Errorf("failed to query auth methods: %w", aErr)
	}
	defer aResp.Body.Close()

	var authData map[string]interface{}
	json.NewDecoder(aResp.Body).Decode(&authData)

	shouldEnable := true
	if methods, ok := authData["data"].(map[string]interface{}); ok {
		if _, exists := methods["approle/"]; exists {
			shouldEnable = false
		}
	} else if _, exists := authData["approle/"]; exists {
		shouldEnable = false
	}

	if shouldEnable {
		authConf := map[string]interface{}{"type": "approle"}
		enResp, enErr := doVaultRequest(ctx, "POST", fmt.Sprintf("%s/v1/sys/auth/approle", vaultURL), rootToken, authConf)
		if enErr == nil && enResp.StatusCode == 204 {
			log.Info("Enabled AppRole authentication engine.")
		}
		if enResp != nil {
			enResp.Body.Close()
		}
	} else {
		log.Info("AppRole authentication engine already enabled.")
	}

	// Policy configuration
	policyStr := `path "secret/data/flume/*" { capabilities = ["read"] }`
	polConf := map[string]interface{}{"policy": policyStr}
	pResp, pErr := doVaultRequest(ctx, "POST", fmt.Sprintf("%s/v1/sys/policies/acl/flume-read-policy", vaultURL), rootToken, polConf)
	if pErr == nil && pResp.StatusCode == 204 {
		pResp.Body.Close()
	} else if pResp != nil {
		pResp.Body.Close()
	}

	// Write Role natively
	roleConf := map[string]interface{}{"token_policies": []string{"flume-read-policy"}}
	rResp, rErr := doVaultRequest(ctx, "POST", fmt.Sprintf("%s/v1/auth/approle/role/flume-worker", vaultURL), rootToken, roleConf)
	if rErr == nil && rResp.StatusCode == 204 {
		rResp.Body.Close()
	} else if rResp != nil {
		rResp.Body.Close()
	}

	// Write Role ID natively
	rIdConf := map[string]interface{}{"role_id": "flume-client-role"}
	idResp, idErr := doVaultRequest(ctx, "POST", fmt.Sprintf("%s/v1/auth/approle/role/flume-worker/role-id", vaultURL), rootToken, rIdConf)
	if idErr == nil && idResp.StatusCode == 204 {
		idResp.Body.Close()
	} else if idResp != nil {
		idResp.Body.Close()
	}

	// Retrieve Secret ID mapping natively
	secResp, secErr := doVaultRequest(ctx, "POST", fmt.Sprintf("%s/v1/auth/approle/role/flume-worker/secret-id", vaultURL), rootToken, nil)
	if secErr != nil {
		return "", fmt.Errorf("failed to fetch secret-id natively: %w", secErr)
	}
	defer secResp.Body.Close()

	var secData struct {
		Data struct {
			SecretId string `json:"secret_id"`
		} `json:"data"`
	}
	if err := json.NewDecoder(secResp.Body).Decode(&secData); err != nil {
		return "", fmt.Errorf("failed to decode secret-id payload natively: %w", err)
	}

	log.Info("Successfully provisioned dynamic AppRole flume-worker seamlessly.")
	return secData.Data.SecretId, nil
}

// DeployVaultTopology sequences the Native HTTP Client bootstrap without containerizing natively.
func DeployVaultTopology(ctx context.Context, vaultPort string, envCfg EnvConfig) (string, string, error) {
	vaultURL := fmt.Sprintf("http://localhost:%s", vaultPort)

	if err := AwaitOpenBao(ctx, vaultURL); err != nil {
		return "", "", err
	}

	rootToken, err := InitializeAndUnseal(ctx, vaultURL)
	if err != nil {
		return "", "", err
	}

	if err := ConfigureSecretsEngine(ctx, vaultURL, rootToken, envCfg); err != nil {
		return "", "", err
	}

	secretID, err := ProvisionAppRole(ctx, vaultURL, rootToken)
	if err != nil {
		return "", "", err
	}

	// Return both the AppRole secret ID (for workers) and the root token
	// (so start.go can inject OPENBAO_TOKEN into the container environment).
	return secretID, rootToken, nil
}
