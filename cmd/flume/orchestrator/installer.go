package orchestrator

import (
	"fmt"
	"os"
	"os/exec"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/charmbracelet/log"
)

// EvaluateAndInstall checks the structural ecology and dynamically pulls missing artifacts natively via OS pipelines.
func EvaluateAndInstall(eco SystemEcology) error {
	var missing []string

	if !eco.HasDocker {
		missing = append(missing, "Docker Desktop")
	}
	if !eco.HasUV {
		missing = append(missing, "uv Python Manager")
	}
	if !eco.HasPython {
		missing = append(missing, "Python 3")
	}
	if !eco.HasGo {
		missing = append(missing, "Go Compiler")
	}
	if !eco.HasElastic {
		missing = append(missing, "Elasticsearch")
	}
	if !eco.HasOpenBao {
		missing = append(missing, "OpenBao Vault")
	}
	if !eco.HasElastro {
		missing = append(missing, "Elastro CLI")
	}

	if len(missing) == 0 {
		return nil
	}

	if !ui.PromptForInstall(missing) {
		return fmt.Errorf("user denied dependency injection protocol")
	}

	for _, dep := range missing {
		fmt.Println(ui.CyberGradient(fmt.Sprintf("⚡️ Patching mainframe... Injecting %s into the local OS bounds...", dep)))
		var cmd *exec.Cmd

		switch dep {
		case "Docker Desktop":
			cmd = exec.Command("brew", "install", "--cask", "docker")
		case "uv Python Manager":
			cmd = exec.Command("sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh")
		case "Python 3":
			cmd = exec.Command("brew", "install", "python")
		case "Go Compiler":
			cmd = exec.Command("brew", "install", "go")
		case "Elasticsearch":
			cmd = exec.Command("sh", "-c", "brew tap elastic/tap && brew install elastic/tap/elasticsearch-full")
		case "OpenBao Vault":
			cmd = exec.Command("sh", "-c", "brew tap hashicorp/tap && brew install hashicorp/tap/vault")
		case "Elastro CLI":
			cmd = exec.Command("sh", "-c", "curl -sSfL https://raw.githubusercontent.com/Fremen-Labs/elastro/main/install.sh | bash")
		}

		if cmd != nil {
			cmd.Stdout = os.Stdout
			cmd.Stderr = os.Stderr
			if err := cmd.Run(); err != nil {
				log.Error(fmt.Sprintf("Failed to permanently bind %s into the OS.", dep), "error", err)
				return err
			}
			if dep == "uv Python Manager" {
				os.Setenv("PATH", os.Getenv("PATH")+":"+os.Getenv("HOME")+"/.local/bin")
			}
			fmt.Println(ui.SuccessBlue(fmt.Sprintf("✅ SUCCESS: %s has been strictly synthesized into the kernel.", dep)))
		}
	}
	return nil
}
