package main

import (
	"fmt"
	"os"

	"github.com/Fremen-Labs/flume/src/gateway"
)

func main() {
	addr := gateway.DefaultAddr()
	if err := gateway.StartGateway(addr); err != nil {
		fmt.Fprintf(os.Stderr, "flume-gateway fatal: %v\n", err)
		os.Exit(1)
	}
}
