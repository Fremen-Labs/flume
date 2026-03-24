package commands

import (
	"fmt"
	"net/http"
	"os/exec"
	"time"

	"github.com/spf13/cobra"
	"github.com/Fremen-Labs/flume/cmd/flume/ui"
)

var DoctorCmd = &cobra.Command{
	Use:   "doctor",
	Short: "Diagnose Flume internal components & swarm health",
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Println("\n" + ui.CyberGradient(":: FLUME ECOSYSTEM TELEMETRY DIAGNOSTICS ::") + "\n")
		
		fmt.Println(ui.NeonGreen("Checking Infrastructure Subsystems..."))
		
		// Docker Check
		if err := exec.Command("docker", "info").Run(); err != nil {
			fmt.Printf("├─ Docker Engine:   %s\n", ui.ErrorRed("Offline or Permission Denied"))
		} else {
			fmt.Printf("├─ Docker Engine:   %s\n", ui.SuccessBlue("Online"))
		}
		
		// Elasticsearch Check
		if checkHTTP("http://localhost:9200") {
			fmt.Printf("├─ Elasticsearch:   %s\n", ui.SuccessBlue("Online (Port 9200)"))
		} else {
			fmt.Printf("├─ Elasticsearch:   %s\n", ui.WarningGold("Offline (Swarm Unbound)"))
		}
		
		// OpenBao Check
		if checkHTTP("http://localhost:8200/v1/sys/health") {
			fmt.Printf("├─ OpenBao Vault:   %s\n", ui.SuccessBlue("Online (Port 8200)"))
		} else {
			fmt.Printf("├─ OpenBao Vault:   %s\n", ui.WarningGold("Offline or Initializing"))
		}
		
		// Dashboard Check
		if checkHTTP("http://localhost:8765/api/settings/llm") {
			fmt.Printf("├─ Flume Dashboard: %s\n", ui.SuccessBlue("Online (Port 8765)"))
		} else {
			fmt.Printf("├─ Flume Dashboard: %s\n", ui.ErrorRed("Offline (App Degraded)"))
		}
		
		fmt.Println(ui.NeonGreen("Diagnostic Run Complete."))
		fmt.Println()
	},
}

func checkHTTP(url string) bool {
	client := http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode >= 200 && resp.StatusCode < 600
}
