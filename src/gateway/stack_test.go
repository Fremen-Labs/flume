package gateway

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestHandleStackStubSkipsDeps(t *testing.T) {
	t.Setenv("FLUME_GATEWAY_STUB", "1")
	t.Setenv("ES_URL", "")
	t.Setenv("OPENBAO_ADDR", "")

	cfg := NewConfig("", time.Second)
	srv := &Server{
		config:       cfg,
		nodeRegistry: NewNodeRegistry("", nil),
	}

	req := httptest.NewRequest(http.MethodGet, "/api/stack", nil)
	rr := httptest.NewRecorder()
	srv.handleStack(rr, req)

	if rr.Code != http.StatusOK {
		t.Fatalf("status %d, body %s", rr.Code, rr.Body.String())
	}

	var body map[string]interface{}
	if err := json.Unmarshal(rr.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body["service"] != "flume-core" {
		t.Errorf("service = %v", body["service"])
	}
	gw, _ := body["gateway"].(map[string]interface{})
	if gw["status"] != "ok" {
		t.Errorf("gateway status = %v", gw["status"])
	}
	es, _ := body["elasticsearch"].(map[string]interface{})
	if es["status"] != "skipped" {
		t.Errorf("elasticsearch status = %v, want skipped", es["status"])
	}
	bao, _ := body["openbao"].(map[string]interface{})
	if bao["status"] != "skipped" {
		t.Errorf("openbao status = %v, want skipped", bao["status"])
	}
}
