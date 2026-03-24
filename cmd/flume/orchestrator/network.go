package orchestrator

import (
	"fmt"
	"net"
	"time"
)

// CheckPortBinds aggressively iterates critical TCP boundaries locally ensuring flawless `docker-compose` topology deployment.
func CheckPortBinds(ports []int) map[int]bool {
	results := make(map[int]bool)
	for _, port := range ports {
		addr := fmt.Sprintf("127.0.0.1:%d", port)
		conn, err := net.DialTimeout("tcp", addr, 1*time.Second)
		if err != nil {
			results[port] = false // Port is absolutely free
		} else {
			conn.Close()
			results[port] = true  // Collision Detected natively
		}
	}
	return results
}
