package commands

import (
	"os"

	"github.com/Fremen-Labs/flume/cmd/flume/orchestrator"
	"gopkg.in/yaml.v3"
)

type MeshConfigYaml struct {
	Mesh struct {
		CloudProviders []struct {
			Provider string  `yaml:"provider"`
			Model    string  `yaml:"model"`
			APIKey   string  `yaml:"api_key"`
			Weight   float64 `yaml:"weight"`
		} `yaml:"cloud_providers"`
		LocalNodes []struct {
			ID       string  `yaml:"id"`
			Host     string  `yaml:"host"`
			Port     string  `yaml:"port"`
			Model    string  `yaml:"model"`
			MemoryGB float64 `yaml:"memory_gb"`
		} `yaml:"local_nodes"`
	} `yaml:"mesh"`
	Elastic struct {
		External  bool   `yaml:"external"`
		URL       string `yaml:"url"`
		VerifyTLS bool   `yaml:"verify_tls"`
	} `yaml:"elastic"`
	Repo struct {
		Type       string `yaml:"type"`       // "github" or "ado"
		TokenEnv   string `yaml:"token_env"`  // Name of environment variable holding token
		AdoOrg     string `yaml:"ado_org"`
	} `yaml:"repo"`
}

func parseMeshConfig(path string) (orchestrator.EnvConfig, error) {
	var envCfg orchestrator.EnvConfig
	data, err := os.ReadFile(path)
	if err != nil {
		return envCfg, err
	}

	var m MeshConfigYaml
	if err := yaml.Unmarshal(data, &m); err != nil {
		return envCfg, err
	}

	for i, cp := range m.Mesh.CloudProviders {
		envCfg.CloudProviders = append(envCfg.CloudProviders, orchestrator.CloudProviderEntry{
			Provider: cp.Provider,
			Model:    cp.Model,
			APIKey:   cp.APIKey,
		})
		
		// Map the first element to the legacy singular fields for OpenBao initialization
		if i == 0 {
			envCfg.Provider = cp.Provider
			envCfg.Model = cp.Model
			envCfg.APIKey = cp.APIKey
		}
	}

	for _, ln := range m.Mesh.LocalNodes {
		envCfg.Nodes = append(envCfg.Nodes, orchestrator.NodeConfigEntry{
			ID:       ln.ID,
			Host:     ln.Host,
			Port:     ln.Port,
			ModelTag: ln.Model,
			MemoryGB: ln.MemoryGB,
		})
	}

	envCfg.ExternalElastic = m.Elastic.External
	envCfg.ESUrl = m.Elastic.URL
	envCfg.ESVerifyTLS = m.Elastic.VerifyTLS

	envCfg.RepoType = m.Repo.Type
	envCfg.ADOOrg = m.Repo.AdoOrg
	if m.Repo.TokenEnv != "" {
		token := os.Getenv(m.Repo.TokenEnv)
		if m.Repo.Type == "github" {
			envCfg.GithubToken = token
		} else if m.Repo.Type == "ado" {
			envCfg.ADOToken = token
		}
	}

	return envCfg, nil
}
