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
	if !eco.HasElastro {
		if _, err := exec.LookPath("pipx"); err != nil {
			missing = append(missing, "pipx Environment")
		}
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
		case "uv Python Manager":
			cmd = exec.Command("sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh")
		case "Python 3":
			cmd = installPackage("python")
		case "Go Compiler":
			cmd = installPackage("go")
		case "pipx Environment":
			cmd = exec.Command("sh", "-c", "python3 -m pip install --user pipx && python3 -m pipx ensurepath")
		case "Elastro CLI":
			cmd = exec.Command("sh", "-c", "curl -sSfL https://raw.githubusercontent.com/Fremen-Labs/elastro/main/install.sh | bash")
		}

		if cmd == nil {
			return fmt.Errorf("fatal execution constraint: No supported package manager (apt, yum, pacman, brew) natively found to explicitly construct the %s pipeline", dep)
		}

		if cmd != nil {
			cmd.Stdout = os.Stdout
			cmd.Stderr = os.Stderr
			if err := cmd.Run(); err != nil {
				log.Error(fmt.Sprintf("Failed to permanently bind %s into the OS.", dep), "error", err)
				return err
			}
			if dep == "uv Python Manager" || dep == "pipx Environment" {
				os.Setenv("PATH", os.Getenv("PATH")+":"+os.Getenv("HOME")+"/.local/bin")
			}
			fmt.Println(ui.SuccessBlue(fmt.Sprintf("✅ SUCCESS: %s has been strictly synthesized into the kernel.", dep)))
		}
	}
	return nil
}

func installPackage(dep string) *exec.Cmd {
	if _, err := exec.LookPath("apt-get"); err == nil {
		switch dep {
		case "docker":
			return exec.Command("sudo", "apt-get", "install", "-y", "docker.io")
		case "python":
			return exec.Command("sudo", "apt-get", "install", "-y", "python3")
		case "go":
			return exec.Command("sudo", "apt-get", "install", "-y", "golang")
		}
	} else if _, err := exec.LookPath("yum"); err == nil {
		switch dep {
		case "docker":
			return exec.Command("sudo", "yum", "install", "-y", "docker")
		case "python":
			return exec.Command("sudo", "yum", "install", "-y", "python3")
		case "go":
			return exec.Command("sudo", "yum", "install", "-y", "golang")
		}
	} else if _, err := exec.LookPath("pacman"); err == nil {
		switch dep {
		case "docker":
			return exec.Command("sudo", "pacman", "-S", "--noconfirm", "docker")
		case "python":
			return exec.Command("sudo", "pacman", "-S", "--noconfirm", "python")
		case "go":
			return exec.Command("sudo", "pacman", "-S", "--noconfirm", "go")
		}
	} else if _, err := exec.LookPath("brew"); err == nil {
		switch dep {
		case "docker":
			return exec.Command("brew", "install", "--cask", "docker")
		case "python":
			return exec.Command("brew", "install", "python")
		case "go":
			return exec.Command("brew", "install", "go")
		}
	}
	return nil
}
