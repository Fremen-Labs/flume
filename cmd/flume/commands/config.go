package commands

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/charmbracelet/log"
	"github.com/spf13/cobra"
)

// knownProviders is the canonical list of supported LLM providers.
// Used for both display and input validation in set-provider.
var knownProviders = []struct {
	num, name, desc string
}{
	{"1", "openai", "OpenAI GPT-4o, GPT-4, GPT-3.5 — cloud API"},
	{"2", "anthropic", "Claude 3.5 Sonnet, Claude 3 Opus — cloud API"},
	{"3", "ollama", "Local Ollama models — self-hosted on this machine"},
	{"4", "exo", "Mac MLX distributed inference — Apple Silicon cluster"},
	{"5", "gemini", "Google Gemini Pro / Flash — cloud API"},
	{"6", "grok", "xAI Grok — cloud API"},
}

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
		for _, p := range knownProviders {
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
		provInput, err := reader.ReadString('\n')
		if err != nil {
			return fmt.Errorf("failed to read provider input: %w", err)
		}
		provInput = strings.TrimSpace(provInput)

		// Build lookup maps from the canonical knownProviders list.
		numToName := make(map[string]string, len(knownProviders))
		nameSet := make(map[string]bool, len(knownProviders))
		for _, p := range knownProviders {
			numToName[p.num] = p.name
			nameSet[p.name] = true
		}

		// Resolve: numeric → name, or validate as a known provider name.
		provider, ok := numToName[provInput]
		if !ok {
			// Accept provider name typed directly, but only if it is known.
			if !nameSet[provInput] {
				return fmt.Errorf(
					"unknown provider '%s': must be a number 1–6 or one of: %s",
					sanitizeForTerminal(provInput),
					strings.Join(func() []string {
						names := make([]string, len(knownProviders))
						for i, p := range knownProviders {
							names[i] = p.name
						}
						return names
					}(), ", "),
				)
			}
			provider = provInput
		}

		fmt.Print(ui.WarningGold("Enter model name (e.g. gpt-4o, leave blank for default): "))
		model, err := reader.ReadString('\n')
		if err != nil {
			return fmt.Errorf("failed to read model input: %w", err)
		}
		model = strings.TrimSpace(model)

		apiKey := ""
		if provider != "ollama" && provider != "exo" {
			fmt.Print(ui.WarningGold("Enter API key (input hidden — press Enter): "))
			apiKey, err = reader.ReadString('\n')
			if err != nil {
				return fmt.Errorf("failed to read API key: %w", err)
			}
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
		if _, err := client.Put("/api/settings/llm", payload); err != nil {
			return fmt.Errorf("failed to update LLM settings: %w", err)
		}
		fmt.Println(ui.SuccessBlue(fmt.Sprintf(
			"LLM provider updated to '%s' (model: '%s').",
			sanitizeForTerminal(provider),
			sanitizeForTerminal(model),
		)))
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
		if _, err := client.Put("/api/settings/system", map[string]string{"es_url": esURL}); err != nil {
			return fmt.Errorf("failed to update ES URL: %w", err)
		}
		fmt.Println(ui.SuccessBlue(fmt.Sprintf("Elasticsearch URL updated to: %s", sanitizeForTerminal(esURL))))
		return nil
	},
}

var configRestartCmd = &cobra.Command{
	Use:   "restart",
	Short: "Restart Flume dashboard services",
	RunE: func(cmd *cobra.Command, args []string) error {
		client := ui.NewFlumeClient()
		if _, err := client.Post("/api/settings/restart-services", nil); err != nil {
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
		return fmt.Errorf("could not fetch LLM settings: %w", err)
	}

	// Non-fatal: warn and continue with partial data if these endpoints fail.
	repos, err := client.Get("/api/settings/repos")
	if err != nil {
		log.Warn("Could not fetch repo settings", "error", err)
	}
	sys, err := client.Get("/api/settings/system")
	if err != nil {
		log.Warn("Could not fetch system settings", "error", err)
	}

	if configJSON {
		out := map[string]any{"llm": llm, "repos": repos, "system": sys}
		b, err := json.MarshalIndent(out, "", "  ")
		if err != nil {
			return fmt.Errorf("failed to serialize config as JSON: %w", err)
		}
		fmt.Println(string(b))
		return nil
	}

	fmt.Println(ui.NeonGreen("  FLUME CONFIGURATION  "))
	fmt.Println()

	// LLM section.
	fmt.Println(ui.WarningGold(" LLM PROVIDER"))
	if llm != nil {
		printConfigField("Provider", stringValFromKeys(llm, "—", "provider"))
		printConfigField("Model", stringValFromKeys(llm, "—", "model"))
		printConfigField("Base URL", stringValFromKeys(llm, "—", "baseUrl", "base_url"))
		apiKey := stringValFromKeys(llm, "", "apiKey", "api_key")
		printConfigField("API Key", maskSecret(apiKey))
	}
	fmt.Println()

	// Repo section.
	fmt.Println(ui.WarningGold(" REPOSITORY"))
	if repos != nil {
		printConfigField("Type", stringValFromKeys(repos, "—", "type", "repoType"))
		printConfigField("Org/User", stringValFromKeys(repos, "—", "org", "adoOrg"))
		tok := stringValFromKeys(repos, "", "token", "githubToken", "adoToken")
		printConfigField("Token", maskSecret(tok))
	} else {
		fmt.Println(ui.WarningGold("  Repo settings unavailable."))
	}
	fmt.Println()

	// System section.
	fmt.Println(ui.WarningGold(" SYSTEM"))
	esURL := "—"
	if sys != nil {
		esURL = stringValFromKeys(sys, "—", "es_url", "esUrl")
	} else {
		fmt.Println(ui.WarningGold("  System settings unavailable."))
	}
	printConfigField("ES URL", esURL)
	printConfigField("Dashboard", fmt.Sprintf("%s (%s)", client.BaseURL, ui.StatusBadge("healthy")))
	fmt.Println()
	return nil
}

func printConfigField(label, value string) {
	fmt.Printf("  %-12s: %s\n", label, value)
}

func init() {
	ConfigCmd.Flags().BoolVarP(&configJSON, "json", "j", false, "Output raw JSON")
	ConfigCmd.AddCommand(configShowCmd)
	ConfigCmd.AddCommand(configProvidersCmd)
	ConfigCmd.AddCommand(configSetProviderCmd)
	ConfigCmd.AddCommand(configSetESURLCmd)
	ConfigCmd.AddCommand(configRestartCmd)
}
