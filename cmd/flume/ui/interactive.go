package ui

import (
	"fmt"
	"strings"

	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/log"
)

type NodeEntry struct {
	ID       string
	Host     string // IP or DNS name (without port)
	Port     string // custom port override; blank = "11434"
	ModelTag string
	MemoryGB string
}

type PromptConfig struct {
	Provider string
	Model    string
	APIKey   string
	Host     string

	ExternalElastic bool
	ElasticURL      string

	RepoType    string
	GithubToken string
	ADOOrg      string
	// ADOProject is collected for optional future use but is NOT used to build
	// ADO connection strings in the current version. Org + PAT are sufficient.
	// It is retained in the struct for forward-compatibility only.
	ADOProject string
	ADOToken   string

	CloudProviders []struct {
		Provider string
		Model    string
		APIKey   string
	}
	Nodes []NodeEntry // Collected during node mesh wizard
}

const (
	StepExoPrompt = iota
	StepProvider
	StepModel
	StepOllamaScope
	StepOllamaIP
	StepAPIKey
	StepCloudMore   // loop back to StepProvider if Y
	StepNodeMesh    // "Add more Ollama nodes?" yes/no
	StepNodeID      // Node ID entry
	StepNodeHost    // Node host (IP/DNS, no port)
	StepNodePort    // "Custom port? (blank = 11434)"
	StepNodeModel   // Model tag for this node
	StepNodeMemory  // Memory (GB)
	StepNodeMore    // "Add another node?" yes/no
	StepElasticMenu
	StepElasticURL
	StepRepoMenu
	StepRepoType
	StepGithubToken
	StepADOOrg
	StepADOToken
	StepDone
)

// providerLabel maps a provider key to its display name used across prompts.
// This is the single source of truth so that all prompts are consistent.
var providerLabel = map[string]string{
	"openai":    "OpenAI",
	"anthropic": "Anthropic",
	"ollama":    "Ollama",
	"exo":       "Exo (Mac MLX)",
	"gemini":    "Gemini",
	"grok":      "Grok",
}

type promptModel struct {
	step      int
	cfg       *PromptConfig
	inputs    map[int]textinput.Model
	exoActive bool
	errMsg    string    // validation error shown inline; cleared on next keypress
	curNode   NodeEntry // node currently being collected
}

// errStyle renders inline validation errors in a distinctive amber/red colour.
var errStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("#FF5F57")).Bold(true)

func renderErr(msg string) string {
	if msg == "" {
		return ""
	}
	return "\n" + errStyle.Render("⚠ "+msg) + "\n"
}

// displayProvider returns a human-readable provider name for the chosen provider.
// Falls back to the raw identifier when no mapping exists (defensive).
func displayProvider(p string) string {
	if label, ok := providerLabel[p]; ok {
		return label
	}
	return p
}

func InitialPromptModel(cfg *PromptConfig, exoActive bool) promptModel {
	m := promptModel{
		cfg:      cfg,
		inputs:   make(map[int]textinput.Model),
		exoActive: exoActive,
	}

	for i := StepExoPrompt; i < StepDone; i++ {
		ti := textinput.New()
		ti.Placeholder = "Enter value..."
		if i == StepAPIKey || i == StepGithubToken || i == StepADOToken {
			ti.EchoMode = textinput.EchoPassword
			ti.EchoCharacter = '*'
		}
		if i == StepElasticURL {
			ti.Placeholder = "http://localhost:9200"
		}
		if i == StepExoPrompt || i == StepElasticMenu || i == StepRepoMenu || i == StepCloudMore {
			ti.Placeholder = "1"
		}
		m.inputs[i] = ti
	}

	if m.exoActive {
		m.step = StepExoPrompt
	} else {
		m.step = StepProvider
	}

	ti := m.inputs[m.step]
	ti.Focus()
	m.inputs[m.step] = ti

	return m
}

func (m promptModel) Init() tea.Cmd {
	return textinput.Blink
}

func (m promptModel) next(nextStep int) (tea.Model, tea.Cmd) {
	ti := m.inputs[m.step]
	ti.Blur()
	ti.ShowSuggestions = false
	m.inputs[m.step] = ti

	m.step = nextStep
	if m.step != StepDone {
		tiNext := m.inputs[m.step]
		tiNext.Focus()
		tiNext.SetValue("")

		// Enable tab-autocomplete on model entry steps.
		if m.step == StepModel || m.step == StepNodeModel {
			prov := m.cfg.Provider
			ollamaHost := m.cfg.Host
			if ollamaHost == "" {
				ollamaHost = "127.0.0.1"
			}

			if m.step == StepNodeModel {
				prov = "ollama"
				if m.curNode.Host != "" {
					ollamaHost = m.curNode.Host
					// Append user's port if available, otherwise Ollama fallback will handle it
					if m.curNode.Port != "" {
						ollamaHost += ":" + m.curNode.Port
					}
				}
			}

			suggestions := ModelSuggestionsForProvider(prov, ollamaHost)
			tiNext.SetSuggestions(suggestions)
			tiNext.ShowSuggestions = true
		}

		m.inputs[m.step] = tiNext
		return m, textinput.Blink
	}
	return m, tea.Quit
}

func (m promptModel) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.KeyMsg:
		// Clear any lingering validation error on the first new keystroke.
		m.errMsg = ""

		if msg.Type == tea.KeyCtrlC || msg.Type == tea.KeyEsc {
			return m, tea.Quit
		}
		if msg.Type == tea.KeyEnter {
			val := strings.TrimSpace(m.inputs[m.step].Value())

			// Validate before advancing.
			if errMsg := validateForStep(m.step, val); errMsg != "" {
				m.errMsg = errMsg
				return m, textinput.Blink
			}

			switch m.step {
			case StepExoPrompt:
				if val == "1" || val == "y" || val == "Y" || val == "" {
					m.cfg.Provider = "exo"
					return m.next(StepModel)
				}
				return m.next(StepProvider)
			case StepProvider:
				switch val {
				case "", "1":
					m.cfg.Provider = "openai"
				case "2":
					m.cfg.Provider = "anthropic"
				case "3":
					m.cfg.Provider = "ollama"
				case "4":
					m.cfg.Provider = "exo"
				case "5":
					m.cfg.Provider = "gemini"
				case "6":
					m.cfg.Provider = "grok"
				default:
					return m, textinput.Blink
				}
				return m.next(StepModel)
			case StepModel:
				m.cfg.Model = val
				if m.cfg.Provider == "ollama" {
					return m.next(StepOllamaScope)
				}
				if m.cfg.Provider == "exo" {
					m.cfg.APIKey = ""
					return m.next(StepElasticMenu)
				}
				return m.next(StepAPIKey)
			case StepOllamaScope:
				if val == "" || val == "1" {
					m.cfg.Host = "127.0.0.1"
					m.cfg.APIKey = ""
					return m.next(StepNodeMesh)
				}
				return m.next(StepOllamaIP)
			case StepOllamaIP:
				if val != "" {
					m.cfg.Host = val
				}
				return m.next(StepCloudMore)
			case StepAPIKey:
				m.cfg.APIKey = val
				return m.next(StepCloudMore)
			case StepCloudMore:
				if m.cfg.Provider != "" {
					if m.cfg.Provider != "ollama" && m.cfg.Provider != "exo" {
						m.cfg.CloudProviders = append(m.cfg.CloudProviders, struct {
							Provider string
							Model    string
							APIKey   string
						}{
							Provider: m.cfg.Provider,
							Model:    m.cfg.Model,
							APIKey:   m.cfg.APIKey,
						})
					}
				}
				if val == "1" || val == "y" || val == "Y" || val == "" {
					m.cfg.Provider = ""
					m.cfg.Model = ""
					m.cfg.APIKey = ""
					return m.next(StepProvider)
				}
				return m.next(StepNodeMesh)
			case StepNodeMesh:
				if val == "1" || val == "y" || val == "Y" || val == "" {
					m.curNode = NodeEntry{}
					return m.next(StepNodeID)
				}
				return m.next(StepElasticMenu)
			case StepNodeID:
				m.curNode.ID = val
				return m.next(StepNodeHost)
			case StepNodeHost:
				m.curNode.Host = val
				return m.next(StepNodePort)
			case StepNodePort:
				if val == "" {
					m.curNode.Port = "11434"
				} else {
					m.curNode.Port = val
				}
				return m.next(StepNodeModel)
			case StepNodeModel:
				m.curNode.ModelTag = val
				return m.next(StepNodeMemory)
			case StepNodeMemory:
				m.curNode.MemoryGB = val
				// Save completed node
				m.cfg.Nodes = append(m.cfg.Nodes, m.curNode)
				return m.next(StepNodeMore)
			case StepNodeMore:
				if val == "1" || val == "y" || val == "Y" || val == "" {
					m.curNode = NodeEntry{}
					return m.next(StepNodeID)
				}
				return m.next(StepElasticMenu)
			case StepElasticMenu:
				if val == "2" {
					m.cfg.ExternalElastic = true
					return m.next(StepElasticURL)
				}
				m.cfg.ExternalElastic = false
				return m.next(StepRepoMenu)
			case StepElasticURL:
				m.cfg.ElasticURL = val
				return m.next(StepRepoMenu)
			case StepRepoMenu:
				if val == "1" {
					return m.next(StepRepoType)
				}
				return m.next(StepDone)
			case StepRepoType:
				if val == "2" {
					m.cfg.RepoType = "ado"
					return m.next(StepADOOrg)
				}
				m.cfg.RepoType = "github"
				return m.next(StepGithubToken)
			case StepGithubToken:
				m.cfg.GithubToken = val
				return m.next(StepDone)
			case StepADOOrg:
				m.cfg.ADOOrg = val
				return m.next(StepADOToken)
			case StepADOToken:
				m.cfg.ADOToken = val
				return m.next(StepDone)
			}
		}
	}

	var cmd tea.Cmd
	ti := m.inputs[m.step]
	ti, cmd = ti.Update(msg)
	m.inputs[m.step] = ti
	return m, cmd
}

func (m promptModel) View() string {
	ti := m.inputs[m.step]
	err := renderErr(m.errMsg)
	// provider label used in dynamic prompts below.
	pLabel := displayProvider(m.cfg.Provider)

	switch m.step {
	case StepExoPrompt:
		return NeonGreen("Exo Mac MLX Inference detected! Route workloads through Exo natively?\n") + "\n1. Yes\n2. No\n\n" + ti.View() + err + "\n(Press enter to continue)\n"
	case StepProvider:
		return NeonGreen("Select LLM Provider by number:\n") + "\n1. openai\n2. anthropic\n3. ollama\n4. exo\n5. gemini\n6. grok\n\n" + ti.View() + err + "\n(Press enter to continue)\n"
	case StepModel:
		return NeonGreen("Enter "+pLabel+" model constraint (e.g. gpt-4o, claude-opus-4-5, qwen2.5-coder:32b):\n") + "\n" + ti.View() + err + "\n" + Dim("(Tab to autocomplete · Enter to confirm)") + "\n"
	case StepOllamaScope:
		return NeonGreen("Ollama detected. Is this model local or remote?\n") + "\n1. Local\n2. Remote\n\n" + ti.View() + err + "\n(Press enter to continue)\n"
	case StepOllamaIP:
		return NeonGreen("Enter the remote Ollama hostname or IP address:\n") + "\n" + ti.View() + err + "\n(Press enter to continue)\n"
	case StepNodeMesh:
		return NeonGreen("Would you like to add more Ollama nodes to the mesh?\n") + "\n1. Yes, add a node\n2. No, continue\n\n" + ti.View() + err + "\n(Press enter to continue)\n"
	case StepNodeID:
		return NeonGreen("Enter a unique Node ID (e.g. mac-mini-1):\n") + "\n" + ti.View() + err + "\n(Press enter to continue)\n"
	case StepNodeHost:
		return NeonGreen("Enter the node IP address or hostname (e.g. 192.168.1.50):\n") + "\n" + ti.View() + err + "\n(Press enter to continue)\n"
	case StepNodePort:
		return NeonGreen("Custom Ollama port? (blank = 11434):\n") + "\n" + ti.View() + err + "\n(Press enter to use default)\n"
	case StepNodeModel:
		return NeonGreen("Primary model tag for this node (e.g. qwen2.5-coder:32b):\n") + "\n" + ti.View() + err + "\n" + Dim("(Tab to autocomplete · Enter to confirm)") + "\n"
	case StepNodeMemory:
		return NeonGreen("Total memory (GB) on this node (e.g. 64):\n") + "\n" + ti.View() + err + "\n(Press enter to continue)\n"
	case StepNodeMore:
		count := len(m.cfg.Nodes)
		return NeonGreen(fmt.Sprintf("%d node(s) registered. Add another?\n", count)) + "\n1. Yes\n2. No, continue\n\n" + ti.View() + err + "\n(Press enter to continue)\n"
	case StepAPIKey:
		return NeonGreen("Enter " + pLabel + " API Secret (Masked natively):\n") + "\n" + ti.View() + err + "\n(Press enter to continue)\n"
	case StepCloudMore:
		return NeonGreen("API Secret secured. Would you like to add another Cloud Provider to the Mesh?\n") + "\n1. Yes, add another Cloud endpoint\n2. No, continue\n\n" + ti.View() + err + "\n(Press enter to continue)\n"
	case StepElasticMenu:
		return NeonGreen("Select Elasticsearch capability:\n") + "\n1. Use default Flume Docker instance\n2. Use an existing External Elastic instance\n\n" + ti.View() + err + "\n(Press enter to continue)\n"
	case StepElasticURL:
		return NeonGreen("Enter External Elasticsearch HTTP routing URL:\n") + "\n" + ti.View() + err + "\n(Press enter to continue)\n"
	case StepRepoMenu:
		return NeonGreen("Would you like to configure remote Version Control natively in the CLI?\n") + "\n1. Yes, add Repo Credentials via CLI\n2. No, I will configure via Local GUI later\n\n" + ti.View() + err + "\n(Press enter to continue)\n"
	case StepRepoType:
		return NeonGreen("Select Source Control Provider:\n") + "\n1. GitHub\n2. Azure DevOps (ADO)\n\n" + ti.View() + err + "\n(Press enter to continue)\n"
	case StepGithubToken:
		return NeonGreen("Enter GitHub Personal Access Token (Masked natively):\n") + "\n" + ti.View() + err + "\n(Press enter to continue)\n"
	case StepADOOrg:
		return NeonGreen("Enter Azure DevOps Organization Name:\n") + "\n" + ti.View() + err + "\n(Press enter to continue)\n"
	case StepADOToken:
		return NeonGreen("Enter Azure DevOps Personal Access Token (Masked natively):\n") + "\n" + ti.View() + err + "\n(Press enter to submit)\n"
	}
	return SuccessBlue("Credentials received securely.")
}

func RunInteractivePrompt(exoActive bool) (PromptConfig, error) {
	cfg := PromptConfig{}
	log.Debug("Initializing BubbleTea Interactive Masked UI routines.")
	p := tea.NewProgram(InitialPromptModel(&cfg, exoActive))
	if _, err := p.Run(); err != nil {
		log.Error("BubbleTea TTY interception failed fatally", "error", err)
		return cfg, err
	}
	return cfg, nil
}
