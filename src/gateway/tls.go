package gateway

import (
	"crypto/tls"
	"os"
	"strings"
)

// insecureSkipVerify reports whether outbound TLS verification is disabled.
// Default is verify-on. Set FLUME_TLS_INSECURE=1 only for local self-signed ES.
func insecureSkipVerify() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("FLUME_TLS_INSECURE"))) {
	case "1", "true", "yes", "on":
		return true
	default:
		return false
	}
}

func tlsClientConfig() *tls.Config {
	if insecureSkipVerify() {
		return &tls.Config{InsecureSkipVerify: true} //nolint:gosec
	}
	return nil
}
