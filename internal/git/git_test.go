package git

import (
	"testing"
)

func TestDetectRepoType(t *testing.T) {
	tests := []struct {
		url      string
		expected string
	}{
		{"", "local"},
		{"/home/user/repo", "local"},
		{"./relative/path", "local"},
		{"~/my-repo", "local"},
		{"git@github.com:org/repo.git", "ssh"},
		{"ssh://git@github.com/org/repo", "ssh"},
		{"https://github.com/org/repo.git", "github"},
		{"https://github.com/Fremen-Labs/flume.git", "github"},
		{"https://dev.azure.com/org/project/_git/repo", "ado"},
		{"https://user@dev.azure.com/org/project/_git/repo", "ado"},
		{"https://org.visualstudio.com/project/_git/repo", "ado"},
		{"https://gitlab.com/org/repo.git", "generic_https"},
		{"http://my-server:3000/repo.git", "generic_https"},
	}

	for _, tt := range tests {
		result := DetectRepoType(tt.url)
		if result != tt.expected {
			t.Errorf("DetectRepoType(%q) = %q, want %q", tt.url, result, tt.expected)
		}
	}
}

func TestParseGitHubOwnerRepo(t *testing.T) {
	tests := []struct {
		url           string
		wantOwner     string
		wantRepo      string
		wantOK        bool
	}{
		{"https://github.com/Fremen-Labs/flume.git", "Fremen-Labs", "flume", true},
		{"https://github.com/Fremen-Labs/flume", "Fremen-Labs", "flume", true},
		{"git@github.com:Fremen-Labs/flume.git", "Fremen-Labs", "flume", true},
		{"git@github.com:Fremen-Labs/flume", "Fremen-Labs", "flume", true},
		{"https://gitlab.com/org/repo", "", "", false},
		{"not-a-url", "", "", false},
	}

	for _, tt := range tests {
		owner, repo, ok := ParseGitHubOwnerRepo(tt.url)
		if ok != tt.wantOK || owner != tt.wantOwner || repo != tt.wantRepo {
			t.Errorf("ParseGitHubOwnerRepo(%q) = (%q, %q, %v), want (%q, %q, %v)",
				tt.url, owner, repo, ok, tt.wantOwner, tt.wantRepo, tt.wantOK)
		}
	}
}

func TestParseADOComponents(t *testing.T) {
	tests := []struct {
		url         string
		wantOrg     string
		wantProject string
		wantRepo    string
		wantOK      bool
	}{
		{
			"https://dev.azure.com/myorg/myproject/_git/myrepo",
			"myorg", "myproject", "myrepo", true,
		},
		{
			"https://user@dev.azure.com/myorg/myproject/_git/myrepo.git",
			"myorg", "myproject", "myrepo", true,
		},
		{
			"https://myorg.visualstudio.com/myproject/_git/myrepo",
			"myorg", "myproject", "myrepo", true,
		},
		{
			"https://myorg.visualstudio.com/DefaultCollection/myproject/_git/myrepo",
			"myorg", "myproject", "myrepo", true,
		},
		{
			"https://github.com/org/repo",
			"", "", "", false,
		},
	}

	for _, tt := range tests {
		org, proj, repo, ok := ParseADOComponents(tt.url)
		if ok != tt.wantOK || org != tt.wantOrg || proj != tt.wantProject || repo != tt.wantRepo {
			t.Errorf("ParseADOComponents(%q) = (%q, %q, %q, %v), want (%q, %q, %q, %v)",
				tt.url, org, proj, repo, ok, tt.wantOrg, tt.wantProject, tt.wantRepo, tt.wantOK)
		}
	}
}

func TestStripCredentials(t *testing.T) {
	tests := []struct {
		input    string
		expected string
	}{
		{
			"https://user:pass@github.com/org/repo.git",
			"https://github.com/org/repo.git",
		},
		{
			"https://github.com/org/repo.git",
			"https://github.com/org/repo.git",
		},
		{
			"https://mentat-automation@dev.azure.com/org/proj/_git/repo",
			"https://dev.azure.com/org/proj/_git/repo",
		},
	}

	for _, tt := range tests {
		result := StripCredentials(tt.input)
		if result != tt.expected {
			t.Errorf("StripCredentials(%q) = %q, want %q", tt.input, result, tt.expected)
		}
	}
}

func TestEmbedCredentials_LocalPassthrough(t *testing.T) {
	local := "/home/user/my-repo"
	result := EmbedCredentials(local, "local")
	if result != local {
		t.Errorf("EmbedCredentials should pass through local paths, got %q", result)
	}
}

func TestEmbedCredentials_SSHPassthrough(t *testing.T) {
	ssh := "git@github.com:org/repo.git"
	result := EmbedCredentials(ssh, "ssh")
	if result != ssh {
		t.Errorf("EmbedCredentials should pass through SSH URLs, got %q", result)
	}
}

func TestHostErrors(t *testing.T) {
	err := &HostError{Message: "test error", Code: 500}
	if err.Error() != "test error" {
		t.Errorf("HostError.Error() = %q, want 'test error'", err.Error())
	}

	authErr := &AuthError{HostError{Message: "auth failed", Code: 401}}
	if authErr.Error() != "auth failed" {
		t.Errorf("AuthError.Error() = %q, want 'auth failed'", authErr.Error())
	}

	notFoundErr := &NotFoundError{HostError{Message: "not found", Code: 404}}
	if notFoundErr.Error() != "not found" {
		t.Errorf("NotFoundError.Error() = %q, want 'not found'", notFoundErr.Error())
	}
}

func TestHelpers(t *testing.T) {
	if strVal(nil) != "" {
		t.Error("strVal(nil) should be empty")
	}
	if strVal("hello") != "hello" {
		t.Error("strVal(hello) should be hello")
	}
	if intVal(nil) != 0 {
		t.Error("intVal(nil) should be 0")
	}
	if intVal(float64(42)) != 42 {
		t.Error("intVal(42.0) should be 42")
	}
	if truncate("hello world", 5) != "hello" {
		t.Error("truncate should work")
	}
	if truncate("hi", 5) != "hi" {
		t.Error("truncate should not truncate short strings")
	}
}
