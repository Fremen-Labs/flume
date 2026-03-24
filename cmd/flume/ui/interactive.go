package ui

import (
	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/log"
)

type PromptConfig struct {
	Provider string
	APIKey   string
}

type promptModel struct {
	providerInput textinput.Model
	keyInput      textinput.Model
	step          int
	cfg           *PromptConfig
}

func InitialPromptModel(cfg *PromptConfig) promptModel {
	pi := textinput.New()
	pi.Placeholder = "openai / anthropic / ollama"
	pi.Focus()

	ki := textinput.New()
	ki.Placeholder = "sk-..."
	ki.EchoMode = textinput.EchoPassword
	ki.EchoCharacter = '•'

	return promptModel{
		providerInput: pi,
		keyInput:      ki,
		step:          0,
		cfg:           cfg,
	}
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
			if m.step == 0 {
				m.cfg.Provider = m.providerInput.Value()
				if m.cfg.Provider == "" {
					m.cfg.Provider = "openai"
				}
				m.step++
				m.providerInput.Blur()
				m.keyInput.Focus()
				return m, textinput.Blink
			} else {
				m.cfg.APIKey = m.keyInput.Value()
				return m, tea.Quit
			}
		}
	}

	if m.step == 0 {
		m.providerInput, cmd = m.providerInput.Update(msg)
	} else {
		m.keyInput, cmd = m.keyInput.Update(msg)
	}
	return m, cmd
}

func (m promptModel) View() string {
	if m.step == 0 {
		return NeonGreen("Select LLM Provider: \n\n") +
			m.providerInput.View() + "\n\n(Press enter to continue)\n"
	}
	if m.step == 1 {
		return NeonGreen("Enter API Secret (Masked natively): \n\n") +
			m.keyInput.View() + "\n\n(Press enter to submit)\n"
	}
	return SuccessBlue("Credentials received securely.")
}

// RunInteractivePrompt renders the Bubbletea TTY sequences intercepting API bindings securely globally.
func RunInteractivePrompt() (PromptConfig, error) {
	cfg := PromptConfig{}
	log.Debug("Initializing BubbleTea Interactive Masked UI routines.")
	p := tea.NewProgram(InitialPromptModel(&cfg))
	if _, err := p.Run(); err != nil {
		log.Error("BubbleTea TTY interception failed fatally", "error", err)
		return cfg, err
	}
	return cfg, nil
}
