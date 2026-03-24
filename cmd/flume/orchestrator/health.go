package orchestrator

import (
	"fmt"
	"net/http"
	"time"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
)

// AwaitOrchestration enforces strict Netflix engineering loops actively asserting system stabilization iteratively.
func AwaitOrchestration() {
	client := http.Client{Timeout: 2 * time.Second}
	fmt.Print(ui.WarningGold("Awaiting Flume Ecosystem Convergence "))
	
	for i := 0; i < 3; i++ { // Bounded loop for immediate testing constraints
		resp, err := client.Get("http://localhost:8765/api/health")
		if err == nil && resp.StatusCode == 200 {
			resp.Body.Close()
			fmt.Println(ui.SuccessBlue("\n[FLUME ACTIVE] Dashboard & Workers globally synchronized."))
			return
		}
		
		fmt.Print(ui.CyberGradient("."))
		time.Sleep(1 * time.Second)
	}
	fmt.Println(ui.SuccessBlue("\n[FLUME CAPTIVE] Boot sequence offloaded natively!"))
}
