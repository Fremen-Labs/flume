package main

import (
	"log/slog"
	"os"

	"github.com/Fremen-Labs/flume/src/gateway"
)

func main() {
	addr := gateway.DefaultAddr()
	if err := gateway.StartGateway(addr); err != nil {
		// InitLogger is called inside StartGateway, so the structured logger
		// is available here even on startup failure. Route through slog so
		// log aggregators (Elastic/Loki/CloudWatch) capture this event as
		// structured JSON rather than raw stderr text.
		gateway.Log().Error("flume-gateway fatal startup failure",
			slog.String("addr", addr),
			slog.String("error", err.Error()),
		)
		os.Exit(1)
	}
}
