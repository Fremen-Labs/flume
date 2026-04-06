package orchestrator

// release.go — GitHub Releases API integration and self-update.
//
// Queries https://api.github.com/repos/Fremen-Labs/flume/releases/latest
// for the latest tagged release, compares against the embedded CurrentVersion
// constant (injected at build time via -ldflags), downloads the platform binary
// asset if a newer version is available, verifies SHA256 checksum, and atomically
// replaces the running binary.

import (
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/charmbracelet/log"
)

// CurrentVersion is injected at build time:
//
//	go build -ldflags "-X github.com/Fremen-Labs/flume/cmd/flume/orchestrator.CurrentVersion=v1.2.3"
//
// Falls back to "dev" when building locally without ldflags.
var CurrentVersion = "dev"

// githubRelease models the fields we need from the GitHub Releases API response.
type githubRelease struct {
	TagName string          `json:"tag_name"`
	Assets  []githubAsset   `json:"assets"`
}

type githubAsset struct {
	Name               string `json:"name"`
	BrowserDownloadURL string `json:"browser_download_url"`
}

// LatestRelease fetches the latest release from GitHub.
// repo format: "Fremen-Labs/flume"
// Returns (tagName, downloadURL, error).  downloadURL is the binary asset for
// the current GOOS/GOARCH (e.g. "flume-linux-amd64", "flume-darwin-arm64").
func LatestRelease(repo string) (string, string, error) {
	url := fmt.Sprintf("https://api.github.com/repos/%s/releases/latest", repo)
	client := &http.Client{Timeout: 15 * time.Second}

	resp, err := client.Get(url)
	if err != nil {
		return "", "", fmt.Errorf("failed to reach GitHub Releases API: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == 404 {
		return "", "", errors.New("no releases found for this repository")
	}
	if resp.StatusCode != 200 {
		return "", "", fmt.Errorf("GitHub API returned HTTP %d", resp.StatusCode)
	}

	var release githubRelease
	if err := json.NewDecoder(resp.Body).Decode(&release); err != nil {
		return "", "", fmt.Errorf("failed to parse GitHub release response: %w", err)
	}

	// Resolve platform asset name: flume-<os>-<arch>
	// e.g. flume-darwin-arm64, flume-linux-amd64
	assetName := fmt.Sprintf("flume-%s-%s", runtime.GOOS, runtime.GOARCH)

	for _, asset := range release.Assets {
		if strings.EqualFold(asset.Name, assetName) || strings.EqualFold(asset.Name, assetName+".tar.gz") {
			return release.TagName, asset.BrowserDownloadURL, nil
		}
	}

	// Asset not found — upgrade is available but we can't auto-apply it
	return release.TagName, "", fmt.Errorf(
		"release %s found but no asset for %s/%s (expected: %s)",
		release.TagName, runtime.GOOS, runtime.GOARCH, assetName,
	)
}

// CompareVersions returns true if latestTag is strictly newer than currentTag.
// Uses simple semver comparison on the vMAJOR.MINOR.PATCH components.
// Returns false if either tag is "dev" or malformed (safe default).
func CompareVersions(currentTag, latestTag string) bool {
	if currentTag == "dev" || latestTag == "dev" {
		return false
	}
	cur := parseSemver(strings.TrimPrefix(currentTag, "v"))
	lat := parseSemver(strings.TrimPrefix(latestTag, "v"))
	for i := 0; i < 3; i++ {
		if lat[i] > cur[i] {
			return true
		}
		if lat[i] < cur[i] {
			return false
		}
	}
	return false // equal
}

// parseSemver splits "1.2.3" into [1, 2, 3].  Returns [0,0,0] on parse error.
func parseSemver(v string) [3]int {
	var result [3]int
	parts := strings.SplitN(v, ".", 3)
	for i, p := range parts {
		if i >= 3 {
			break
		}
		var n int
		fmt.Sscanf(strings.TrimSpace(p), "%d", &n)
		result[i] = n
	}
	return result
}

// SelfUpdate downloads the new binary from downloadURL, verifies the SHA256
// checksum (fetched from <downloadURL>.sha256 alongside the asset), and
// atomically replaces the running executable.
//
// If the checksum file is not present the update proceeds without verification
// and a warning is logged (allows shipping without checksum files during early
// release cycles).
func SelfUpdate(downloadURL string) error {
	log.Infof("Downloading update from %s", downloadURL)

	client := &http.Client{Timeout: 5 * time.Minute}

	// 1. Download new binary
	resp, err := client.Get(downloadURL)
	if err != nil {
		return fmt.Errorf("download failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("download HTTP %d", resp.StatusCode)
	}

	// Write to a temp file in the same directory as the current executable
	// so that the rename is atomic (same filesystem).
	exePath, err := os.Executable()
	if err != nil {
		return fmt.Errorf("cannot determine executable path: %w", err)
	}
	exeDir := filepath.Dir(exePath)

	tmpFile, err := os.CreateTemp(exeDir, ".flume-upgrade-*")
	if err != nil {
		return fmt.Errorf("cannot create temp file for update: %w", err)
	}
	defer os.Remove(tmpFile.Name()) // clean up on any error path

	hasher := sha256.New()
	tee := io.TeeReader(resp.Body, hasher)
	if _, err := io.Copy(tmpFile, tee); err != nil {
		tmpFile.Close()
		return fmt.Errorf("failed to write downloaded binary: %w", err)
	}
	tmpFile.Close()

	// 2. Verify checksum (best-effort — skip if no .sha256 file provided)
	checksumURL := downloadURL + ".sha256"
	csResp, csErr := client.Get(checksumURL)
	if csErr == nil && csResp.StatusCode == 200 {
		defer csResp.Body.Close()
		csBytes, _ := io.ReadAll(csResp.Body)
		expectedHash := strings.TrimSpace(strings.Fields(string(csBytes))[0])
		actualHash := fmt.Sprintf("%x", hasher.Sum(nil))
		if !strings.EqualFold(expectedHash, actualHash) {
			return fmt.Errorf("checksum mismatch: expected %s, got %s", expectedHash, actualHash)
		}
		log.Info("Binary checksum verified.")
	} else {
		log.Warn("No checksum file found — skipping verification.")
	}

	// 3. Make executable
	if err := os.Chmod(tmpFile.Name(), 0755); err != nil {
		return fmt.Errorf("chmod failed: %w", err)
	}

	// 4. Atomic replace: rename temp over current executable
	if err := os.Rename(tmpFile.Name(), exePath); err != nil {
		return fmt.Errorf("failed to replace binary (may need sudo): %w", err)
	}

	log.Infof("Binary updated successfully: %s", exePath)
	return nil
}
