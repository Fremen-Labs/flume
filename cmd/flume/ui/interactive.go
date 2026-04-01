package ui

import (
	"strings"

	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/log"
)

type PromptConfig struct {
	Provider       string
	Model          string
	APIKey         string
	Host           string

	ExternalElastic bool
	ElasticURL      string

	RepoType    string
	GithubToken string
	ADOOrg      string
	ADOProject  string
	ADOToken    string
}

const (
	StepExoPrompt = iota
	StepProvider
	StepModel
	StepOllamaScope
	StepOllamaIP
	StepAPIKey
	StepElasticMenu
	StepElasticURL
	StepRepoMenu
	StepRepoType
	StepGithubToken
	StepADOOrg
	StepADOProject
	StepADOToken
	StepDone
)

type promptModel struct {
	step      int
	cfg       *PromptConfig
	inputs    map[int]textinput.Model
	exoActive bool
}

func InitialPromptModel(cfg *PromptConfig, exoActive bool) promptModel {
	m := promptModel{
		cfg:       cfg,
		inputs:    make(map[int]textinput.Model),
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
		if i == StepExoPrompt || i == StepElasticMenu || i == StepRepoMenu {
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
	m.inputs[m.step] = ti

	m.step = nextStep
	if m.step != StepDone {
		tiNext := m.inputs[m.step]
		tiNext.Focus()
		m.inputs[m.step] = tiNext
		return m, textinput.Blink
	}
	return m, tea.Quit
}

func (m promptModel) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.KeyMsg:
		if msg.Type == tea.KeyCtrlC || msg.Type == tea.KeyEsc {
			return m, tea.Quit
		}
		if msg.Type == tea.KeyEnter {
			val := strings.TrimSpace(m.inputs[m.step].Value())
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
					return m.next(StepElasticMenu)
				}
				return m.next(StepOllamaIP)
			case StepOllamaIP:
				if val != "" {
					m.cfg.Host = val
				}
				return m.next(StepElasticMenu)
			case StepAPIKey:
				m.cfg.APIKey = val
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
				return m.next(StepADOProject)
			case StepADOProject:
				m.cfg.ADOProject = val
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
	switch m.step {
	case StepExoPrompt:
		return NeonGreen("Exo Mac MLX Inference detected! Route workloads through Exo natively?\n") + "\n1. Yes\n2. No\n\n" + ti.View() + "\n\n(Press enter to continue)\n"
	case StepProvider:
		return NeonGreen("Select LLM Provider by number:\n") + "\n1. openai\n2. anthropic\n3. ollama\n4. exo\n\n" + ti.View() + "\n\n(Press enter to continue)\n"
	case StepModel:
		return NeonGreen("Enter specific LLM Model constraint (e.g. qwen, llama3.2):\n") + "\n" + ti.View() + "\n\n(Press enter to continue)\n"
	case StepOllamaScope:
		return NeonGreen("Ollama detected. Is this model local or remote?\n") + "\n1. Local\n2. Remote\n\n" + ti.View() + "\n\n(Press enter to continue)\n"
	case StepOllamaIP:
		return NeonGreen("Enter the remote Ollama hostname or IP address:\n") + "\n" + ti.View() + "\n\n(Press enter to continue)\n"
	case StepAPIKey:
		return NeonGreen("Enter API Secret (Masked natively):\n") + "\n" + ti.View() + "\n\n(Press enter to continue)\n"
	case StepElasticMenu:
		return NeonGreen("Select Elasticsearch capability:\n") + "\n1. Use default Flume Docker instance\n2. Use an existing External Elastic instance\n\n" + ti.View() + "\n\n(Press enter to continue)\n"
	case StepElasticURL:
		return NeonGreen("Enter External Elasticsearch HTTP routing URL:\n") + "\n" + ti.View() + "\n\n(Press enter to continue)\n"
	case StepRepoMenu:
		return NeonGreen("Would you like to configure remote Version Control natively in the CLI?\n") + "\n1. Yes, add Repo Credentials via CLI\n2. No, I will configure via Local GUI later\n\n" + ti.View() + "\n\n(Press enter to continue)\n"
	case StepRepoType:
		return NeonGreen("Select Source Control Provider:\n") + "\n1. GitHub\n2. Azure DevOps (ADO)\n\n" + ti.View() + "\n\n(Press enter to continue)\n"
	case StepGithubToken:
		return NeonGreen("Enter GitHub Personal Access Token:\n") + "\n" + ti.View() + "\n\n(Press enter to continue)\n"
	case StepADOOrg:
		return NeonGreen("Enter Azure DevOps Organization Name:\n") + "\n" + ti.View() + "\n\n(Press enter to continue)\n"
	case StepADOProject:
		return NeonGreen("Enter Azure DevOps Project Name:\n") + "\n" + ti.View() + "\n\n(Press enter to continue)\n"
	case StepADOToken:
		return NeonGreen("Enter Azure DevOps Personal Access Token (Masked):\n") + "\n" + ti.View() + "\n\n(Press enter to submit)\n"
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
