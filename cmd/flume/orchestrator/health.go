package orchestrator

import (
	"fmt"
	"net/http"
	"time"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
)

// AwaitOrchestration enforces strict Netflix engineering loops actively asserting system stabilization iteratively.
func AwaitOrchestration() error {
	client := http.Client{Timeout: 2 * time.Second}
	fmt.Print(ui.WarningGold("Awaiting Flume Ecosystem Convergence "))
	
	for i := 0; i < 60; i++ { // 60-second robust back-off loop
		resp, err := client.Get("http://localhost:8765/api/health")
		if err == nil && resp.StatusCode == 200 {
			resp.Body.Close()
			fmt.Println(ui.SuccessBlue("\n[FLUME ACTIVE] Dashboard & Workers globally synchronized."))
			return nil
		}
		
		fmt.Print(ui.CyberGradient("."))
		time.Sleep(1 * time.Second)
	}
	fmt.Println(ui.CyberGradient("\n[ERROR] Boot sequence timeout out after 60 seconds!"))
	return fmt.Errorf("timeout awaiting flume ecosystem convergence")
}
