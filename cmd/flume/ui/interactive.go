package ui

import (
	"strings"

	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/log"
)

type PromptConfig struct {
	Provider       string
	APIKey         string
	Host           string
	ChangeProvider bool
}

type promptModel struct {
	changeInput   textinput.Model
	providerInput textinput.Model
	choiceInput   textinput.Model
	keyInput      textinput.Model
	hostInput     textinput.Model
	step          int
	cfg           *PromptConfig
}

func InitialPromptModel(cfg *PromptConfig) promptModel {
	changeInput := textinput.New()
	changeInput.Placeholder = "y or n"

	providerInput := textinput.New()
	providerInput.Placeholder = "1"

	choiceInput := textinput.New()
	choiceInput.Placeholder = "1"

	keyInput := textinput.New()
	keyInput.Placeholder = "sk-..."
	keyInput.EchoMode = textinput.EchoPassword
	keyInput.EchoCharacter = '*'

	hostInput := textinput.New()
	hostInput.Placeholder = "hostname or ip"

	model := promptModel{
		changeInput:   changeInput,
		providerInput: providerInput,
		choiceInput:   choiceInput,
		keyInput:      keyInput,
		hostInput:     hostInput,
		step:          0,
		cfg:           cfg,
	}

	if strings.TrimSpace(cfg.Provider) != "" {
		model.changeInput.Focus()
	} else {
		model.providerInput.Focus()
		model.step = 1
	}

	return model
}

func (m promptModel) Init() tea.Cmd {
	return textinput.Blink
}

func (m promptModel) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	var cmd tea.Cmd

	switch msg := msg.(type) {
	case tea.KeyMsg:
		switch msg.Type {
		case tea.KeyCtrlC, tea.KeyEsc:
			return m, tea.Quit
		case tea.KeyEnter:
			switch m.step {
			case 0:
				choice := strings.TrimSpace(strings.ToLower(m.changeInput.Value()))
				if choice == "" || choice == "n" || choice == "no" {
					m.cfg.ChangeProvider = false
					return m, tea.Quit
				}
				m.cfg.ChangeProvider = true
				m.step = 1
				m.changeInput.Blur()
				m.providerInput.SetValue("")
				m.providerInput.Focus()
				return m, textinput.Blink
			case 1:
				switch strings.TrimSpace(m.providerInput.Value()) {
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
				if m.cfg.Provider == "ollama" {
					m.cfg.APIKey = ""
					m.step = 2
					m.providerInput.Blur()
					m.choiceInput.SetValue("")
					m.choiceInput.Focus()
					return m, textinput.Blink
				}
				m.step = 4
				m.providerInput.Blur()
				m.keyInput.Focus()
				return m, textinput.Blink
			case 2:
				choice := strings.TrimSpace(m.choiceInput.Value())
				if choice == "" || choice == "1" {
					m.cfg.Host = "127.0.0.1"
					m.cfg.APIKey = ""
					return m, tea.Quit
				}
				if choice == "2" {
					m.step = 3
					m.choiceInput.Blur()
					m.hostInput.SetValue("")
					m.hostInput.Focus()
					return m, textinput.Blink
				}
				return m, textinput.Blink
			case 3:
				if strings.TrimSpace(m.hostInput.Value()) == "" {
					return m, textinput.Blink
				}
				m.cfg.Host = strings.TrimSpace(m.hostInput.Value())
				m.cfg.APIKey = ""
				return m, tea.Quit
			default:
				m.cfg.APIKey = m.keyInput.Value()
				return m, tea.Quit
			}
		}
	}

	switch m.step {
	case 0:
		m.changeInput, cmd = m.changeInput.Update(msg)
	case 1:
		m.providerInput, cmd = m.providerInput.Update(msg)
	case 2:
		m.choiceInput, cmd = m.choiceInput.Update(msg)
	case 3:
		m.hostInput, cmd = m.hostInput.Update(msg)
	default:
		m.keyInput, cmd = m.keyInput.Update(msg)
	}
	return m, cmd
}

func (m promptModel) View() string {
	if m.step == 0 && strings.TrimSpace(m.cfg.Provider) != "" {
		return NeonGreen("Existing .env LLM provider detected: "+m.cfg.Provider+"\n\n") +
			m.changeInput.View() + "\n" +
			"Change the LLM provider? (y/N)\n\n" +
			"(Press enter to continue)\n"
	}
	if m.step == 1 {
		return NeonGreen("Select LLM Provider by number:\n\n") +
			m.providerInput.View() + "\n\n" +
			"1. openai\n" +
			"2. anthropic\n" +
			"3. ollama\n" +
			"4. exo\n\n" +
			"(Press enter to continue)\n"
	}
	if m.step == 2 {
		return NeonGreen("Ollama detected. Is this model local or remote?\n\n") +
			m.choiceInput.View() + "\n\n" +
			"1. Local\n" +
			"2. Remote\n\n" +
			"(Press enter to continue)\n"
	}
	if m.step == 3 {
		return NeonGreen("Enter the remote Ollama hostname or IP address:\n\n") +
			m.hostInput.View() + "\n\n" +
			"(Press enter to continue)\n"
	}
	if m.step == 4 {
		return NeonGreen("Enter API Secret (Masked natively): \n\n") +
			m.keyInput.View() + "\n\n(Press enter to submit)\n"
	}
	return SuccessBlue("Credentials received securely.")
}

// RunInteractivePrompt renders the Bubbletea TTY sequences intercepting API bindings securely globally.
func RunInteractivePrompt(initial ...PromptConfig) (PromptConfig, error) {
	cfg := PromptConfig{}
	if len(initial) > 0 {
		cfg = initial[0]
	}
	log.Debug("Initializing BubbleTea Interactive Masked UI routines.")
	p := tea.NewProgram(InitialPromptModel(&cfg))
	if _, err := p.Run(); err != nil {
		log.Error("BubbleTea TTY interception failed fatally", "error", err)
		return cfg, err
	}
	return cfg, nil
}
