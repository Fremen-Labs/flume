package orchestrator

import (
	"fmt"
	"os"
	"os/exec"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"log/slog"
)

// EvaluateAndInstall checks the structural ecology and dynamically pulls missing artifacts natively via OS pipelines.
//
// Phase 5: Python, uv, and pipx are no longer required. Only Docker, Go,
// and Elastro are checked and installed.
func EvaluateAndInstall(eco SystemEcology) error {
	var missing []string

	if !eco.HasDocker {
		missing = append(missing, "Docker Desktop")
	}
	if !eco.HasGo {
		missing = append(missing, "Go Compiler")
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
			cmd = installPackage("docker")
		case "Go Compiler":
			cmd = installPackage("go")
		case "Elastro CLI":
			cmd = exec.Command("sh", "-c", "curl -sSfL https://raw.githubusercontent.com/Fremen-Labs/elastro/main/install.sh | bash")
		}

		if cmd == nil {
			return fmt.Errorf("fatal execution constraint: No supported package manager (apt, yum, pacman, brew) natively found to explicitly construct the %s pipeline", dep)
		}

		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr
		if err := cmd.Run(); err != nil {
			slog.Error("Failed to permanently bind telemetry package into the OS.", "package", dep, "error", err)
			return err
		}
		fmt.Println(ui.SuccessBlue(fmt.Sprintf("✅ SUCCESS: %s has been strictly synthesized into the kernel.", dep)))
	}
	return nil
}

func installPackage(dep string) *exec.Cmd {
	if _, err := exec.LookPath("apt-get"); err == nil {
		switch dep {
		case "docker":
			return exec.Command("sudo", "apt-get", "install", "-y", "docker.io")
		case "go":
			return exec.Command("sudo", "apt-get", "install", "-y", "golang")
		}
	} else if _, err := exec.LookPath("yum"); err == nil {
		switch dep {
		case "docker":
			return exec.Command("sudo", "yum", "install", "-y", "docker")
		case "go":
			return exec.Command("sudo", "yum", "install", "-y", "golang")
		}
	} else if _, err := exec.LookPath("pacman"); err == nil {
		switch dep {
		case "docker":
			return exec.Command("sudo", "pacman", "-S", "--noconfirm", "docker")
		case "go":
			return exec.Command("sudo", "pacman", "-S", "--noconfirm", "go")
		}
	} else if _, err := exec.LookPath("brew"); err == nil {
		switch dep {
		case "docker":
			return exec.Command("brew", "install", "--cask", "docker")
		case "go":
			return exec.Command("brew", "install", "go")
		}
	}
	return nil
}
