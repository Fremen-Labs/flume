package secrets

import (
	"crypto/rand"
	"fmt"
)

// GenerateShortID returns a 12-char hex string from crypto/rand.
// Used for credential and token IDs across all stores.
func GenerateShortID() string {
	b := make([]byte, 6)
	if _, err := rand.Read(b); err != nil {
		// Extremely unlikely; fall back to timestamp-based
		return fmt.Sprintf("%012x", 0)
	}
	return fmt.Sprintf("%x", b)
}
