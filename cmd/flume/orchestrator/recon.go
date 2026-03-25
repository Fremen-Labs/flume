package orchestrator

import (
	"os/exec"
)

type SystemEcology struct {
	HasDocker      bool
	HasElastic     bool
	HasOpenBao     bool
	HasFlumeLegacy bool
	HasElastro     bool
	HasUV          bool
	HasPython      bool
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
		HasUV:          checkBinary("uv"),
		HasPython:      checkBinary("python3") || checkBinary("python"),
		HasGo:          checkBinary("go"),
	}
}

func checkBinary(name string) bool {
	_, err := exec.LookPath(name)
	return err == nil
}
