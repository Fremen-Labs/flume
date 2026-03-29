package orchestrator

import (
	"net/http"
	"os/exec"
	"time"
)

// CheckExoActive natively identifies if Mac Unified Memory Exo hardware MLX interfaces are securely online natively!
func CheckExoActive() bool {
	// Execute binary lookup
	_, err := exec.LookPath("exo")
	if err == nil {
		return true
	}

	// Probe Exo native inference API metrics globally
	client := http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get("http://localhost:52415/api/v1/models")
	if err == nil {
		defer resp.Body.Close()
		return resp.StatusCode == 200
	}
	return false
}
