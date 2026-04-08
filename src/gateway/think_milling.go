package gateway

import (
	"bytes"
	"strings"
)

// ─────────────────────────────────────────────────────────────────────────────
// Think Milling — streaming state machine that strips <think>...</think>
// blocks from model output in real-time.
//
// The name "milling" reflects the mechanical process of cutting away
// unwanted material (think tokens) to produce a clean finished surface
// (visible content).
//
// Operates on byte slices with zero allocations per chunk using bytes.Buffer.
// Handles tag boundaries that split across chunk boundaries.
// ─────────────────────────────────────────────────────────────────────────────

// ThinkMill strips <think>...</think> blocks from streamed content.
type ThinkMill struct {
	inThink  bool
	buf      bytes.Buffer // partial tag accumulator
	visible  bytes.Buffer // accumulated visible content
}

// NewThinkMill creates a new ThinkMill.
func NewThinkMill() *ThinkMill {
	return &ThinkMill{}
}

var (
	thinkOpen  = []byte("<think>")
	thinkClose = []byte("</think>")
)

// Process feeds a chunk of content through the milling state machine.
// Returns any newly visible content extracted from this chunk.
func (m *ThinkMill) Process(chunk []byte) []byte {
	if len(chunk) == 0 {
		return nil
	}

	// Prepend any buffered partial tag
	var combined []byte
	if m.buf.Len() > 0 {
		combined = make([]byte, m.buf.Len()+len(chunk))
		copy(combined, m.buf.Bytes())
		copy(combined[m.buf.Len():], chunk)
		m.buf.Reset()
	} else {
		combined = chunk
	}

	var visibleChunk bytes.Buffer

	for len(combined) > 0 {
		if m.inThink {
			// Scanning for </think>
			idx := bytes.Index(combined, thinkClose)
			if idx == -1 {
				// Check for partial </think> at the end
				for i := 1; i < len(thinkClose) && i <= len(combined); i++ {
					if bytes.HasSuffix(combined, thinkClose[:i]) {
						m.buf.Write(combined[len(combined)-i:])
						combined = combined[:len(combined)-i]
						break
					}
				}
				// Everything else is inside <think>, discard
				combined = nil
			} else {
				m.inThink = false
				combined = combined[idx+len(thinkClose):]
			}
		} else {
			// Scanning for <think>
			idx := bytes.Index(combined, thinkOpen)
			if idx == -1 {
				// Check for partial <think> at the end
				for i := 1; i < len(thinkOpen) && i <= len(combined); i++ {
					if bytes.HasSuffix(combined, thinkOpen[:i]) {
						m.buf.Write(combined[len(combined)-i:])
						combined = combined[:len(combined)-i]
						break
					}
				}
				// Everything remaining is visible
				visibleChunk.Write(combined)
				combined = nil
			} else {
				// Content before <think> is visible
				visibleChunk.Write(combined[:idx])
				m.inThink = true
				combined = combined[idx+len(thinkOpen):]
			}
		}
	}

	if visibleChunk.Len() > 0 {
		m.visible.Write(visibleChunk.Bytes())
		return visibleChunk.Bytes()
	}
	return nil
}

// Visible returns all accumulated visible content.
func (m *ThinkMill) Visible() string {
	return strings.TrimSpace(m.visible.String())
}

// Reset clears the mill state for reuse.
func (m *ThinkMill) Reset() {
	m.inThink = false
	m.buf.Reset()
	m.visible.Reset()
}
