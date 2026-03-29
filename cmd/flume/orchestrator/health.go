package orchestrator

import (
	"fmt"
	"net/http"
	"os"
	"time"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
)

const (
	healthCheckTimeout  = 2 * time.Second
	healthCheckRetries  = 60
	healthCheckInterval = 1 * time.Second
)

type HealthStatus struct {
	Active  bool
	Timeout bool
}

// PollHealth iteratively checks the health endpoint and pushes state to a channel.
func PollHealth(url string) <-chan HealthStatus {
	ch := make(chan HealthStatus)
	go func() {
		defer close(ch)
		client := http.Client{Timeout: healthCheckTimeout}
		for i := 0; i < healthCheckRetries; i++ {
			resp, err := client.Get(url)
			if err == nil && resp.StatusCode == 200 {
				resp.Body.Close()
				ch <- HealthStatus{Active: true, Timeout: false}
				return
			}
			ch <- HealthStatus{Active: false, Timeout: false}
			time.Sleep(healthCheckInterval)
		}
		ch <- HealthStatus{Active: false, Timeout: true}
	}()
	return ch
}

// AwaitOrchestration enforces strict Netflix engineering loops actively asserting system stabilization iteratively.
func AwaitOrchestration() error {
	healthUrl := os.Getenv("FLUME_HEALTH_URL")
	if healthUrl == "" {
		healthUrl = "http://localhost:8765/api/health"
	}

	fmt.Print(ui.WarningGold("Awaiting Flume Ecosystem Convergence "))
	
	statusCh := PollHealth(healthUrl)
	for status := range statusCh {
		if status.Active {
			fmt.Println(ui.SuccessBlue("\n[FLUME ACTIVE] Dashboard & Workers globally synchronized."))
			return nil
		}
		if status.Timeout {
			fmt.Println(ui.CyberGradient("\n[ERROR] Boot sequence timeout out after 60 seconds!"))
			return fmt.Errorf("timeout awaiting flume ecosystem convergence")
		}
		fmt.Print(ui.CyberGradient("."))
	}
	return nil
}
