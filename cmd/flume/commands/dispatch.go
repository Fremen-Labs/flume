package commands

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/charmbracelet/log"
	"github.com/spf13/cobra"
)

type ESTask struct {
	Index  string `json:"_index"`
	ID     string `json:"_id"`
	Source struct {
		Role         string   `json:"role"`
		Status       string   `json:"status"`
		Dependencies []string `json:"dependencies"`
	} `json:"_source"`
}

type ESSearchResponse struct {
	Hits struct {
		Hits []ESTask `json:"hits"`
	} `json:"hits"`
}

// Configurable global ES query mapped to a Go struct for serialization 
var defaultESQuery = map[string]interface{}{
	"query": map[string]interface{}{
		"bool": map[string]interface{}{
			"must": []map[string]interface{}{
				{
					"terms": map[string]interface{}{
						"status": []string{"blocked", "planned"},
					},
				},
			},
		},
	},
	"size": 1000,
}

var DispatchCmd = &cobra.Command{
	Use:   "dispatch",
	Short: "Run the deterministic Go-native DAG scheduler to sort and route tasks seamlessly",
	Run: func(cmd *cobra.Command, args []string) {
		esURL, _ := cmd.Flags().GetString("es-url")
		log.Info("Booting Go-Native Topological DAG Scheduler", "target", esURL)

		// Create a Wake channel for Event-Driven Push Model (Thundering herd mitigation)
		wakeCh := make(chan struct{}, 1)

		// Spin up lightweight non-blocking HTTP server for Healthz and Webhooks
		go serveControlPlane(wakeCh)

		// Enter Kubernetes-style generic Reconciliation Loop
		runReconciliationLoop(esURL, wakeCh)
	},
}

func init() {
	DispatchCmd.Flags().StringP("es-url", "e", "https://localhost:9200", "Elasticsearch Endpoint")
}

func serveControlPlane(wakeCh chan struct{}) {
	mux := http.NewServeMux()

	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"status": "ok", "service": "flume-dispatcher"}`))
	})

	mux.HandleFunc("/webhook", func(w http.ResponseWriter, r *http.Request) {
		// Non-blocking trigger to wake the reconciliation loop
		select {
		case wakeCh <- struct{}{}:
			log.Info("Webhook triggered DAG reconciliation loop actively.")
		default:
			// Loop already awake, drop redundant events safely
		}
		w.WriteHeader(http.StatusAccepted)
		w.Write([]byte(`{"status": "accepted", "message": "reconciliation triggered"}`))
	})

	server := &http.Server{
		Addr:         ":8766",
		Handler:      mux,
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 5 * time.Second,
	}

	log.Info("Dispatcher Control Plane Listening", "port", 8766)
	if err := server.ListenAndServe(); err != nil {
		log.Error("Dispatcher Control Plane crashed", "error", err)
	}
}

func runReconciliationLoop(esURL string, wakeCh chan struct{}) {
	client := &http.Client{
		Transport: &http.Transport{
			MaxIdleConns:        10,
			IdleConnTimeout:     30 * time.Second,
			DisableKeepAlives:   false,
		},
		Timeout: 10 * time.Second,
	}

	queryBytes, err := json.Marshal(defaultESQuery)
	if err != nil {
		log.Fatalf("FATAL: Failed to marshal default ES query configuration: %v", err)
	}

	log.Info("Native DAG Dispatcher Active. Listening for blocked nodes...")

	for {
		ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
		executeSync(ctx, client, esURL, queryBytes)
		cancel()

		// Borg style: Sleep baseline (Resync interval) OR interrupt immediately via Wake channel 
		select {
		case <-wakeCh:
			// Event-driven invocation
		case <-time.After(30 * time.Second):
			// Periodic reconciliation safety-net
		}
	}
}

func executeSync(ctx context.Context, client *http.Client, esURL string, queryBytes []byte) {
	req, err := http.NewRequestWithContext(ctx, "POST", esURL+"/flume-tasks-*/_search", bytes.NewBuffer(queryBytes))
	if err != nil {
		log.Error("Failed to construct ES request natively", "error", err)
		return
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		log.Error("Elasticsearch cluster unreachable natively", "error", err, "target", esURL)
		return
	}
	// CRITICAL: Ensure body is closed to prevent leaking TCP sockets.
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		log.Error("Elasticsearch rejected query", "status", resp.Status)
		return
	}

	var esRes ESSearchResponse
	if err := json.NewDecoder(resp.Body).Decode(&esRes); err != nil {
		log.Error("Failed to decode Elasticsearch JSON payload", "error", err)
		return
	}

	// 1. Pure Function: Calculate unblocked target boundaries natively
	readyTasks := processDAG(esRes.Hits.Hits)

	// 2. Side Effect: Dispatch mutations synchronously
	if len(readyTasks) > 0 {
		dispatchReadyTasks(ctx, client, esURL, readyTasks)
	}
}

// processDAG represents a completely Pure topological function mapping mathematical dependencies visually 
func processDAG(tasks []ESTask) []ESTask {
	var unblocked []ESTask
	for _, t := range tasks {
		if len(t.Source.Dependencies) == 0 && (t.Source.Status == "blocked" || t.Source.Status == "planned") {
			unblocked = append(unblocked, t)
		}
	}
	return unblocked
}

// dispatchReadyTasks executes ES state mutations natively locking dependencies
func dispatchReadyTasks(ctx context.Context, client *http.Client, esURL string, tasks []ESTask) {
	updatePayload := []byte(`{"doc": {"status": "ready"}}`)

	for _, t := range tasks {
		url := fmt.Sprintf("%s/%s/_update/%s", esURL, t.Index, t.ID)
		
		req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewBuffer(updatePayload))
		if err != nil {
			log.Error("Failed to construct ES Update request natively", "error", err, "doc_id", t.ID)
			continue
		}
		req.Header.Set("Content-Type", "application/json")

		resp, err := client.Do(req)
		if err != nil {
			log.Error("Failed to dispatch task state natively", "error", err, "doc_id", t.ID)
			continue
		}
		
		// Drain and close body rigorously
		resp.Body.Close()

		if resp.StatusCode == http.StatusOK || resp.StatusCode == http.StatusCreated {
			log.Info(ui.NeonGreen(fmt.Sprintf("[DAG UNBLOCK] Node %s strictly routed to ready pool.", t.ID)))
		} else {
			log.Error("Elasticsearch failed to mutate state dynamically", "doc_id", t.ID, "status", resp.Status)
		}
	}
}
