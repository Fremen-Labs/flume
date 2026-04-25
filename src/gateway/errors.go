package gateway

import (
	"fmt"
	"strings"
)

// ─────────────────────────────────────────────────────────────────────────────
// Typed errors for structured provider error classification.
//
// Replaces fragile string-contains checks with proper Go error typing so
// callers can use errors.As to determine the correct HTTP status code.
// ─────────────────────────────────────────────────────────────────────────────

// ProviderError represents a classifiable error from a downstream LLM provider.
// Handlers use errors.As(err, &pe) to extract the HTTP status code instead
// of string-matching against error messages.
type ProviderError struct {
	// HTTPStatus is the status code to return to the gateway client.
	HTTPStatus int
	// Provider is the provider that generated the error (e.g., "anthropic").
	Provider string
	// Message is the human-readable error description.
	Message string
	// Cause is the original wrapped error.
	Cause error
}

// Error implements the error interface.
func (e *ProviderError) Error() string {
	if e.Cause != nil {
		return fmt.Sprintf("%s (provider=%s, status=%d): %v", e.Message, e.Provider, e.HTTPStatus, e.Cause)
	}
	return fmt.Sprintf("%s (provider=%s, status=%d)", e.Message, e.Provider, e.HTTPStatus)
}

// Unwrap supports errors.Is / errors.As chains.
func (e *ProviderError) Unwrap() error {
	return e.Cause
}

// ClassifyProviderError inspects a raw error string and wraps it in a
// ProviderError with the appropriate HTTP status code. This centralises
// the classification logic that was previously duplicated across handlers.
func ClassifyProviderError(err error, provider string) *ProviderError {
	msg := err.Error()
	status := 502 // default: bad gateway

	switch {
	case strings.Contains(msg, "HTTP 402"),
		strings.Contains(msg, "credit_exhausted"),
		strings.Contains(msg, "insufficient_quota"):
		status = 402

	case strings.Contains(msg, "HTTP 429"):
		status = 429

	case strings.Contains(msg, "HTTP 401"),
		strings.Contains(msg, "api key unavailable"):
		status = 401

	case strings.Contains(msg, "HTTP 404"):
		status = 404

	case strings.Contains(msg, "HTTP 400"):
		status = 400
	}

	return &ProviderError{
		HTTPStatus: status,
		Provider:   provider,
		Message:    msg,
		Cause:      err,
	}
}
