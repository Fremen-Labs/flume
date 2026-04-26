package orchestrator

// credstore.go — AES-256-GCM encrypted credential snapshot.
//
// Written by `flume start` after every successful bootstrap.
// Read by `flume upgrade` to restore credentials non-interactively.
//
// Encryption key is derived from machine identity (hostname + OS username) via
// HKDF-SHA256.  The key never leaves the machine.  Works on Linux and macOS
// with zero external dependencies — uses only Go stdlib crypto primitives.
//
// File location: ~/.flume/credentials.enc  (mode 0600)

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/user"
	"path/filepath"
)

const credstoreFile = "credentials.enc"

// credPayload is the plaintext structure serialised into the snapshot.
// All fields are stored; sensitive values (tokens, keys) are encrypted at rest.
type credPayload struct {
	Provider           string `json:"provider"`
	Model              string `json:"model"`
	BaseURL            string `json:"base_url"`
	LocalOllamaBaseURL string `json:"local_ollama_base_url"`
	Host               string `json:"host"`
	APIKey             string `json:"api_key"`
	RepoType           string `json:"repo_type"`
	GithubToken        string `json:"github_token"`
	ADOOrg             string `json:"ado_org"`
	ADOProject         string `json:"ado_project"`
	ADOToken           string `json:"ado_token"`
	ExternalElastic    bool   `json:"external_elastic"`
	ESUrl              string `json:"es_url"`
	ElasticPassword    string `json:"elastic_password"`
	AdminToken         string `json:"admin_token"`
}

// credstorePath returns ~/.flume/credentials.enc.
func credstorePath() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("cannot resolve home directory: %w", err)
	}
	return filepath.Join(home, ".flume", credstoreFile), nil
}

// deriveKey produces a 32-byte AES-256 key from machine identity.
//
// Manual HKDF-like derivation using stdlib HMAC-SHA256 only (no x/crypto dep):
//  1. Extract: PRK = HMAC-SHA256(salt="flume-credstore-v1", ikm=hostname:username)
//  2. Expand:  key = HMAC-SHA256(PRK, info="expand")[:32]
//
// The key is deterministic for a given hostname + username combination and
// survives flume binary updates.
func deriveKey() ([]byte, error) {
	hostname, err := os.Hostname()
	if err != nil {
		hostname = "flume-host"
	}
	usr, err := user.Current()
	username := "flume-user"
	if err == nil {
		username = usr.Username
	}

	ikm := []byte(hostname + ":" + username)
	salt := []byte("flume-credstore-v1")

	// Step 1 — Extract: PRK = HMAC-SHA256(salt, IKM)
	extractor := hmac.New(sha256.New, salt)
	extractor.Write(ikm)
	prk := extractor.Sum(nil)

	// Step 2 — Expand: OKM = HMAC-SHA256(PRK, info || 0x01)
	expander := hmac.New(sha256.New, prk)
	expander.Write([]byte("expand\x01"))
	okm := expander.Sum(nil) // 32 bytes — exactly AES-256 key size

	return okm, nil
}

// SaveCredentials encrypts and writes the EnvConfig credential snapshot to disk.
// Non-fatal: callers should log a warning on error but not abort.
func SaveCredentials(cfg EnvConfig) error {
	payload := credPayload{
		Provider:           cfg.Provider,
		Model:              cfg.Model,
		BaseURL:            cfg.BaseURL,
		LocalOllamaBaseURL: cfg.LocalOllamaBaseURL,
		Host:               cfg.Host,
		APIKey:             cfg.APIKey,
		RepoType:           cfg.RepoType,
		GithubToken:        cfg.GithubToken,
		ADOOrg:             cfg.ADOOrg,
		ADOProject:         cfg.ADOProject,
		ADOToken:           cfg.ADOToken,
		ExternalElastic:    cfg.ExternalElastic,
		ESUrl:              cfg.ESUrl,
		ElasticPassword:    cfg.ElasticPassword,
		AdminToken:         cfg.AdminToken,
	}

	plain, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("credential serialisation failed: %w", err)
	}

	key, err := deriveKey()
	if err != nil {
		return err
	}

	block, err := aes.NewCipher(key)
	if err != nil {
		return fmt.Errorf("AES init failed: %w", err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return fmt.Errorf("GCM init failed: %w", err)
	}

	nonce := make([]byte, gcm.NonceSize())
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return fmt.Errorf("nonce generation failed: %w", err)
	}

	// Ciphertext layout: [nonce] + [GCM-sealed payload]
	ciphertext := gcm.Seal(nonce, nonce, plain, nil)

	path, err := credstorePath()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return fmt.Errorf("cannot create ~/.flume directory: %w", err)
	}
	if err := os.WriteFile(path, ciphertext, 0600); err != nil {
		return fmt.Errorf("failed to write credential snapshot: %w", err)
	}
	return nil
}

// LoadCredentials decrypts and deserialises the credential snapshot.
// Returns ErrNoSnapshot (os.ErrNotExist) when the file doesn't exist so callers
// can distinguish "missing file" from "corrupt file".
func LoadCredentials() (EnvConfig, error) {
	path, err := credstorePath()
	if err != nil {
		return EnvConfig{}, err
	}

	ciphertext, err := os.ReadFile(path)
	if err != nil {
		return EnvConfig{}, fmt.Errorf("credential snapshot not found (%s): %w", path, os.ErrNotExist)
	}

	key, err := deriveKey()
	if err != nil {
		return EnvConfig{}, err
	}

	block, err := aes.NewCipher(key)
	if err != nil {
		return EnvConfig{}, fmt.Errorf("AES init failed: %w", err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return EnvConfig{}, fmt.Errorf("GCM init failed: %w", err)
	}

	nonceSize := gcm.NonceSize()
	if len(ciphertext) < nonceSize {
		return EnvConfig{}, fmt.Errorf("credential snapshot is corrupt (too short)")
	}
	nonce, sealed := ciphertext[:nonceSize], ciphertext[nonceSize:]

	plain, err := gcm.Open(nil, nonce, sealed, nil)
	if err != nil {
		return EnvConfig{}, fmt.Errorf("credential decryption failed (wrong machine or corrupt file): %w", err)
	}

	var payload credPayload
	if err := json.Unmarshal(plain, &payload); err != nil {
		return EnvConfig{}, fmt.Errorf("credential deserialisation failed: %w", err)
	}

	return EnvConfig{
		Provider:           payload.Provider,
		Model:              payload.Model,
		BaseURL:            payload.BaseURL,
		LocalOllamaBaseURL: payload.LocalOllamaBaseURL,
		Host:               payload.Host,
		APIKey:             payload.APIKey,
		RepoType:           payload.RepoType,
		GithubToken:        payload.GithubToken,
		ADOOrg:             payload.ADOOrg,
		ADOProject:         payload.ADOProject,
		ADOToken:           payload.ADOToken,
		ExternalElastic:    payload.ExternalElastic,
		ESUrl:              payload.ESUrl,
		ElasticPassword:    payload.ElasticPassword,
		AdminToken:         payload.AdminToken,
	}, nil
}
