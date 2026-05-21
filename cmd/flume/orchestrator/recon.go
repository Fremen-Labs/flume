package orchestrator

import (
	"os/exec"
)

// SystemEcology captures the local OS binary dependency state.
//
// Phase 5: Python, uv, and pipx are no longer required. All application
// services compile into a single Go binary. Only Docker (for ES + OpenBao
// infrastructure containers) and Go (for building the binary) are needed.
type SystemEcology struct {
	HasDocker      bool
	HasElastic     bool
	HasOpenBao     bool
	HasFlumeLegacy bool
	HasElastro     bool
	HasGo          bool
}

// PerformReconnaissance dynamically evaluates the local OS for existing binary dependencies explicitly tracking
// Elastic, OpenBao, `elastro` nodes, and Docker desktop to gracefully suppress nested container composition!
func PerformReconnaissance() SystemEcology {
	return SystemEcology{
		HasDocker:      checkBinary("docker"),
		HasElastic:     checkBinary("elasticsearch"),
		HasOpenBao:     checkBinary("openbao") || checkBinary("vault"),
		HasFlumeLegacy: checkBinary("flume"),
		HasElastro:     checkBinary("elastro"),
		HasGo:          checkBinary("go"),
	}
}

func checkBinary(name string) bool {
	_, err := exec.LookPath(name)
	return err == nil
}
