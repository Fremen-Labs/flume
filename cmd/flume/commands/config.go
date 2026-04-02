package commands

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/spf13/cobra"
)

var configJSON bool

var ConfigCmd = &cobra.Command{
	Use:   "config",
	Short: "Manage Flume LLM, Elasticsearch, and system configuration",
	Long:  `View or update LLM provider, model, API credentials, and Elasticsearch settings.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		return showConfig()
	},
}

var configShowCmd = &cobra.Command{
	Use:   "show",
	Short: "Show the current Flume configuration (secrets masked)",
	RunE: func(cmd *cobra.Command, args []string) error {
		return showConfig()
	},
}

var configProvidersCmd = &cobra.Command{
	Use:   "providers",
	Short: "List all available LLM providers",
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Println(ui.NeonGreen("  AVAILABLE LLM PROVIDERS  "))
		providers := []struct{ num, name, desc string }{
			{"1", "openai", "OpenAI GPT-4o, GPT-4, GPT-3.5 — cloud API"},
			{"2", "anthropic", "Claude 3.5 Sonnet, Claude 3 Opus — cloud API"},
			{"3", "ollama", "Local Ollama models — self-hosted on this machine"},
			{"4", "exo", "Mac MLX distributed inference — Apple Silicon cluster"},
			{"5", "gemini", "Google Gemini Pro / Flash — cloud API"},
			{"6", "grok", "xAI Grok — cloud API"},
		}
		for _, p := range providers {
			fmt.Printf("  %s. %-12s → %s\n", p.num, ui.SuccessBlue(p.name), p.desc)
		}
	},
}

var configSetProviderCmd = &cobra.Command{
	Use:   "set-provider",
	Short: "Interactively update the LLM provider and credentials",
	RunE: func(cmd *cobra.Command, args []string) error {
		reader := bufio.NewReader(os.Stdin)

		fmt.Print(ui.WarningGold("Enter provider number (1-6): "))
		provInput, _ := reader.ReadString('\n')
		provInput = strings.TrimSpace(provInput)
		providerMap := map[string]string{
			"1": "openai", "2": "anthropic", "3": "ollama", "4": "exo", "5": "gemini", "6": "grok",
		}
		provider, ok := providerMap[provInput]
		if !ok {
			provider = provInput // allow typing the name directly
		}

		fmt.Print(ui.WarningGold("Enter model name (e.g. gpt-4o, leave blank for default): "))
		model, _ := reader.ReadString('\n')
		model = strings.TrimSpace(model)

		apiKey := ""
		if provider != "ollama" && provider != "exo" {
			fmt.Print(ui.WarningGold("Enter API key (input masked — press Enter): "))
			apiKey, _ = reader.ReadString('\n')
			apiKey = strings.TrimSpace(apiKey)
		}

		client := ui.NewFlumeClient()
		payload := map[string]any{
			"provider": provider,
			"model":    model,
		}
		if apiKey != "" {
			payload["apiKey"] = apiKey
		}
		result, err := client.Put("/api/settings/llm", payload)
		if err != nil {
			return fmt.Errorf("failed to update LLM settings: %w", err)
		}
		_ = result
		fmt.Println(ui.SuccessBlue(fmt.Sprintf("LLM provider updated to '%s' (model: '%s').", provider, model)))
		return nil
	},
}

var configSetESURLCmd = &cobra.Command{
	Use:   "set-es-url <url>",
	Short: "Update the Elasticsearch endpoint URL",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		esURL := args[0]
		if !strings.HasPrefix(esURL, "http://") && !strings.HasPrefix(esURL, "https://") {
			return fmt.Errorf("URL must start with http:// or https://")
		}
		client := ui.NewFlumeClient()
		result, err := client.Put("/api/settings/system", map[string]string{"es_url": esURL})
		if err != nil {
			return fmt.Errorf("failed to update ES URL: %w", err)
		}
		_ = result
		fmt.Println(ui.SuccessBlue(fmt.Sprintf("Elasticsearch URL updated to: %s", esURL)))
		return nil
	},
}

var configRestartCmd = &cobra.Command{
	Use:   "restart",
	Short: "Restart Flume dashboard services",
	RunE: func(cmd *cobra.Command, args []string) error {
		client := ui.NewFlumeClient()
		_, err := client.Post("/api/settings/restart-services", nil)
		if err != nil {
			return fmt.Errorf("failed to restart services: %w", err)
		}
		fmt.Println(ui.SuccessBlue("Services restarting..."))
		return nil
	},
}

func showConfig() error {
	client := ui.NewFlumeClient()

	llm, err := client.Get("/api/settings/llm")
	if err != nil {
		return fmt.Errorf("dashboard unreachable: %w", err)
	}
	repos, _ := client.Get("/api/settings/repos")
	sys, _ := client.Get("/api/settings/system")

	if configJSON {
		out := map[string]any{"llm": llm, "repos": repos, "system": sys}
		b, _ := json.MarshalIndent(out, "", "  ")
		fmt.Println(string(b))
		return nil
	}

	fmt.Println(ui.NeonGreen("  FLUME CONFIGURATION  "))
	fmt.Println()

	// LLM section.
	fmt.Println(ui.WarningGold(" LLM PROVIDER"))
	if llm != nil {
		printConfigField("Provider", stringVal(llm, "provider", "—"))
		printConfigField("Model", stringVal(llm, "model", "—"))
		printConfigField("Base URL", stringVal(llm, "baseUrl", stringVal(llm, "base_url", "—")))
		apiKey := stringVal(llm, "apiKey", stringVal(llm, "api_key", ""))
		printConfigField("API Key", maskSecret(apiKey))
	}
	fmt.Println()

	// Repo section.
	fmt.Println(ui.WarningGold(" REPOSITORY"))
	if repos != nil {
		printConfigField("Type", stringVal(repos, "type", stringVal(repos, "repoType", "—")))
		printConfigField("Org/User", stringVal(repos, "org", stringVal(repos, "adoOrg", "—")))
		tok := stringVal(repos, "token", stringVal(repos, "githubToken", ""))
		printConfigField("Token", maskSecret(tok))
	}
	fmt.Println()

	// System section.
	fmt.Println(ui.WarningGold(" SYSTEM"))
	esURL := "—"
	if sys != nil {
		esURL = stringVal(sys, "es_url", stringVal(sys, "esUrl", "—"))
	}
	printConfigField("ES URL", esURL)
	printConfigField("Dashboard", fmt.Sprintf("%s (%s)", client.BaseURL, ui.StatusBadge("healthy")))
	fmt.Println()
	return nil
}

func printConfigField(label, value string) {
	fmt.Printf("  %-12s: %s\n", label, value)
}

func maskSecret(s string) string {
	if s == "" {
		return "—"
	}
	if len(s) <= 8 {
		return "***MASKED***"
	}
	return s[:8] + "***MASKED***"
}

func init() {
	ConfigCmd.Flags().BoolVarP(&configJSON, "json", "j", false, "Output raw JSON")
	ConfigCmd.AddCommand(configShowCmd)
	ConfigCmd.AddCommand(configProvidersCmd)
	ConfigCmd.AddCommand(configSetProviderCmd)
	ConfigCmd.AddCommand(configSetESURLCmd)
	ConfigCmd.AddCommand(configRestartCmd)
}
