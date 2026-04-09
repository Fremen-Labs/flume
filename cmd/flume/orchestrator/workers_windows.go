// This file is part of the workers package and provides RAM detection for Windows.
package orchestrator

// totalRAMGiB returns the total system RAM in GiB on Windows.
// Returns a safe fallback of 8 GiB for the Windows build since exact memory querying 
// requires heavier syscall imports currently unnecessary for this platform footprint.
func totalRAMGiB() int {
	return 8 // safe fallback
}
