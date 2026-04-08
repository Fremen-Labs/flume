package gateway

import (
	"testing"
)

func TestThinkMillBasic(t *testing.T) {
	m := NewThinkMill()
	result := m.Process([]byte("Hello world"))
	if string(result) != "Hello world" {
		t.Errorf("expected 'Hello world', got %q", string(result))
	}
}

func TestThinkMillStripsBlock(t *testing.T) {
	m := NewThinkMill()
	m.Process([]byte("<think>internal reasoning</think>The answer is 42."))
	if m.Visible() != "The answer is 42." {
		t.Errorf("expected 'The answer is 42.', got %q", m.Visible())
	}
}

func TestThinkMillMultiChunk(t *testing.T) {
	m := NewThinkMill()
	m.Process([]byte("<thi"))
	m.Process([]byte("nk>lots of thinking"))
	m.Process([]byte("</thi"))
	m.Process([]byte("nk>visible content"))

	if m.Visible() != "visible content" {
		t.Errorf("expected 'visible content', got %q", m.Visible())
	}
}

func TestThinkMillMultipleBlocks(t *testing.T) {
	m := NewThinkMill()
	m.Process([]byte("before<think>thought1</think>middle<think>thought2</think>after"))

	if m.Visible() != "beforemiddleafter" {
		t.Errorf("expected 'beforemiddleafter', got %q", m.Visible())
	}
}

func TestThinkMillNoThinkBlocks(t *testing.T) {
	m := NewThinkMill()
	m.Process([]byte("Just normal text without any think blocks"))

	expected := "Just normal text without any think blocks"
	if m.Visible() != expected {
		t.Errorf("expected %q, got %q", expected, m.Visible())
	}
}

func TestThinkMillReset(t *testing.T) {
	m := NewThinkMill()
	m.Process([]byte("<think>first</think>visible1"))
	if m.Visible() != "visible1" {
		t.Fatalf("pre-reset: expected 'visible1', got %q", m.Visible())
	}

	m.Reset()
	m.Process([]byte("visible2"))
	if m.Visible() != "visible2" {
		t.Errorf("post-reset: expected 'visible2', got %q", m.Visible())
	}
}
