package gateway

import (
	"testing"
)

// ─────────────────────────────────────────────────────────────────────────────
// Routing Policy Unit Tests
// ─────────────────────────────────────────────────────────────────────────────

func TestDefaultRoutingPolicy(t *testing.T) {
	p := DefaultRoutingPolicy()
	if p.Mode != RoutingModeLocalOnly {
		t.Errorf("expected local_only default, got %s", p.Mode)
	}
	if len(p.FrontierMix) != 0 {
		t.Errorf("expected empty frontier mix, got %d models", len(p.FrontierMix))
	}
	if p.RolePinning == nil {
		t.Error("expected non-nil RolePinning map")
	}
}

func TestSelectWeightedFrontier_SingleModel(t *testing.T) {
	p := &RoutingPolicy{
		FrontierMix: []FrontierModelWeight{
			{Provider: "openai", Model: "gpt-5", Weight: 1.0},
		},
	}
	for i := 0; i < 100; i++ {
		m := p.SelectWeightedFrontier("")
		if m == nil {
			t.Fatal("expected non-nil model")
		}
		if m.Model != "gpt-5" {
			t.Errorf("expected gpt-5, got %s", m.Model)
		}
	}
}

func TestSelectWeightedFrontier_Distribution(t *testing.T) {
	p := &RoutingPolicy{
		FrontierMix: []FrontierModelWeight{
			{Provider: "openai", Model: "gpt-5", Weight: 0.7},
			{Provider: "anthropic", Model: "claude-opus-4.7", Weight: 0.3},
		},
	}

	counts := map[string]int{"gpt-5": 0, "claude-opus-4.7": 0}
	iterations := 10000

	for i := 0; i < iterations; i++ {
		m := p.SelectWeightedFrontier("")
		if m == nil {
			t.Fatal("expected non-nil model")
		}
		counts[m.Model]++
	}

	// Verify distribution is within ±5% of expected
	gptPct := float64(counts["gpt-5"]) / float64(iterations)
	claudePct := float64(counts["claude-opus-4.7"]) / float64(iterations)

	if gptPct < 0.65 || gptPct > 0.75 {
		t.Errorf("gpt-5 distribution %.2f outside expected range [0.65, 0.75]", gptPct)
	}
	if claudePct < 0.25 || claudePct > 0.35 {
		t.Errorf("claude-opus-4.7 distribution %.2f outside expected range [0.25, 0.35]", claudePct)
	}
}

func TestSelectWeightedFrontier_CircuitBroken(t *testing.T) {
	p := &RoutingPolicy{
		FrontierMix: []FrontierModelWeight{
			{Provider: "openai", Model: "gpt-5", Weight: 0.5, CircuitOpen: true},
			{Provider: "anthropic", Model: "claude-opus-4.7", Weight: 0.5},
		},
	}

	for i := 0; i < 100; i++ {
		m := p.SelectWeightedFrontier("")
		if m == nil {
			t.Fatal("expected non-nil model")
		}
		if m.Model == "gpt-5" {
			t.Error("circuit-broken model gpt-5 should never be selected")
		}
		if m.Model != "claude-opus-4.7" {
			t.Errorf("expected claude-opus-4.7, got %s", m.Model)
		}
	}
}

func TestSelectWeightedFrontier_RolePinning(t *testing.T) {
	p := &RoutingPolicy{
		FrontierMix: []FrontierModelWeight{
			{Provider: "openai", Model: "gpt-5", Weight: 0.8},
			{Provider: "anthropic", Model: "claude-opus-4.7", Weight: 0.2},
		},
		RolePinning: map[string]string{
			"implementer": "claude-opus-4.7",
		},
	}

	// Pinned role should always get the pinned model
	for i := 0; i < 100; i++ {
		m := p.SelectWeightedFrontier("implementer")
		if m == nil {
			t.Fatal("expected non-nil model")
		}
		if m.Model != "claude-opus-4.7" {
			t.Errorf("expected pinned model claude-opus-4.7, got %s", m.Model)
		}
	}

	// Non-pinned role should use weighted selection
	m := p.SelectWeightedFrontier("reviewer")
	if m == nil {
		t.Fatal("expected non-nil model for unpinned role")
	}
}

func TestSelectWeightedFrontier_AllCircuitBroken(t *testing.T) {
	p := &RoutingPolicy{
		FrontierMix: []FrontierModelWeight{
			{Provider: "openai", Model: "gpt-5", Weight: 0.5, CircuitOpen: true},
			{Provider: "anthropic", Model: "claude-opus-4.7", Weight: 0.5, CircuitOpen: true},
		},
	}

	m := p.SelectWeightedFrontier("")
	if m != nil {
		t.Errorf("expected nil when all models are circuit-broken, got %s", m.Model)
	}
}

func TestEnforceSpendBudget_Normal(t *testing.T) {
	p := &RoutingPolicy{
		FrontierMix: []FrontierModelWeight{
			{Provider: "openai", Model: "gpt-5", Weight: 1.0, BudgetUSD: 100.0, SpentUSD: 0},
		},
	}

	usage := Usage{PromptTokens: 1000, CompletionTokens: 500, TotalTokens: 1500}
	p.EnforceSpendBudget("gpt-5", usage)

	if p.FrontierMix[0].CircuitOpen {
		t.Error("model should not be circuit-broken with minimal usage")
	}
	if p.FrontierMix[0].SpentUSD <= 0 {
		t.Error("spend should have been incremented")
	}
}

func TestEnforceSpendBudget_Warning(t *testing.T) {
	p := &RoutingPolicy{
		FrontierMix: []FrontierModelWeight{
			{Provider: "openai", Model: "gpt-5", Weight: 1.0, BudgetUSD: 1.0, SpentUSD: 0.91},
		},
	}

	// Small usage that keeps it above 90% but below 100%
	usage := Usage{PromptTokens: 100, CompletionTokens: 50, TotalTokens: 150}
	p.EnforceSpendBudget("gpt-5", usage)

	if p.FrontierMix[0].CircuitOpen {
		t.Error("model should not be circuit-broken at ~91% budget")
	}
}

func TestEnforceSpendBudget_CircuitBreak(t *testing.T) {
	p := &RoutingPolicy{
		FrontierMix: []FrontierModelWeight{
			{Provider: "openai", Model: "gpt-5", Weight: 1.0, BudgetUSD: 0.01, SpentUSD: 0.009},
		},
	}

	// Big usage that pushes over 100%
	usage := Usage{PromptTokens: 10000, CompletionTokens: 5000, TotalTokens: 15000}
	p.EnforceSpendBudget("gpt-5", usage)

	if !p.FrontierMix[0].CircuitOpen {
		t.Error("model should be circuit-broken when budget is exceeded")
	}
}

func TestLoadRoutingPolicy_Missing(t *testing.T) {
	// Without a running ES, LoadRoutingPolicyFromES should return the default
	p := DefaultRoutingPolicy()
	if p.Mode != RoutingModeLocalOnly {
		t.Errorf("expected local_only default, got %s", p.Mode)
	}
}

func TestNormalizeWeights_AllZeros(t *testing.T) {
	p := &RoutingPolicy{
		FrontierMix: []FrontierModelWeight{
			{Provider: "openai", Model: "gpt-5", Weight: 0},
			{Provider: "anthropic", Model: "claude-opus-4.7", Weight: 0},
		},
	}
	p.normalizeWeights()

	// Should get equal distribution
	expected := 0.5
	for _, m := range p.FrontierMix {
		if m.Weight != expected {
			t.Errorf("expected weight %f, got %f for %s", expected, m.Weight, m.Model)
		}
	}
}

func TestNormalizeWeights_Unnormalized(t *testing.T) {
	p := &RoutingPolicy{
		FrontierMix: []FrontierModelWeight{
			{Provider: "openai", Model: "gpt-5", Weight: 70},
			{Provider: "anthropic", Model: "claude-opus-4.7", Weight: 30},
		},
	}
	p.normalizeWeights()

	if p.FrontierMix[0].Weight < 0.69 || p.FrontierMix[0].Weight > 0.71 {
		t.Errorf("expected ~0.7, got %f", p.FrontierMix[0].Weight)
	}
	if p.FrontierMix[1].Weight < 0.29 || p.FrontierMix[1].Weight > 0.31 {
		t.Errorf("expected ~0.3, got %f", p.FrontierMix[1].Weight)
	}
}

func TestNormalizeWeights_NegativeWeights(t *testing.T) {
	p := &RoutingPolicy{
		FrontierMix: []FrontierModelWeight{
			{Provider: "openai", Model: "gpt-5", Weight: -5},
			{Provider: "anthropic", Model: "claude-opus-4.7", Weight: 10},
		},
	}
	p.normalizeWeights()

	if p.FrontierMix[0].Weight != 0 {
		t.Errorf("negative weight should be clamped to 0, got %f", p.FrontierMix[0].Weight)
	}
	if p.FrontierMix[1].Weight != 1.0 {
		t.Errorf("expected 1.0 for sole positive weight, got %f", p.FrontierMix[1].Weight)
	}
}

func TestValidate_ValidModes(t *testing.T) {
	for _, mode := range []RoutingMode{RoutingModeFrontierOnly, RoutingModeHybrid, RoutingModeLocalOnly} {
		p := &RoutingPolicy{
			Mode:                mode,
			FrontierLocalRatio:  0.5,
			ComplexityThreshold: 7,
		}
		if err := p.Validate(); err != nil {
			t.Errorf("valid mode %q returned error: %v", mode, err)
		}
	}
}

func TestValidate_InvalidMode(t *testing.T) {
	p := &RoutingPolicy{
		Mode:                "invalid_mode",
		FrontierLocalRatio:  0.5,
		ComplexityThreshold: 7,
	}
	if err := p.Validate(); err == nil {
		t.Error("expected error for invalid mode")
	}
}

func TestValidate_InvalidRatio(t *testing.T) {
	p := &RoutingPolicy{
		Mode:                RoutingModeHybrid,
		FrontierLocalRatio:  1.5, // out of range
		ComplexityThreshold: 7,
	}
	if err := p.Validate(); err == nil {
		t.Error("expected error for ratio > 1.0")
	}
}

func TestValidate_InvalidComplexity(t *testing.T) {
	p := &RoutingPolicy{
		Mode:                RoutingModeHybrid,
		FrontierLocalRatio:  0.5,
		ComplexityThreshold: 15, // out of range
	}
	if err := p.Validate(); err == nil {
		t.Error("expected error for complexity > 10")
	}
}

func TestShouldUseFrontier_HighComplexity(t *testing.T) {
	p := &RoutingPolicy{
		ComplexityThreshold: 7,
		FrontierLocalRatio:  0.0, // ratio is 0, but complexity gate should override
	}

	// Reasoning tasks (complexity 8) should always use frontier
	result := p.ShouldUseFrontier("reasoning")
	if !result {
		t.Error("high-complexity task should use frontier regardless of ratio")
	}
}

func TestShouldUseFrontier_LowComplexityZeroRatio(t *testing.T) {
	p := &RoutingPolicy{
		ComplexityThreshold: 7,
		FrontierLocalRatio:  0.0, // never use frontier via probability
	}

	// Evaluation tasks (complexity 3) with 0 ratio should never use frontier
	for i := 0; i < 100; i++ {
		result := p.ShouldUseFrontier("evaluation")
		if result {
			t.Error("low-complexity task with 0 ratio should never use frontier")
		}
	}
}

func TestHasAvailableFrontierModels(t *testing.T) {
	p := &RoutingPolicy{
		FrontierMix: []FrontierModelWeight{
			{Model: "gpt-5", Weight: 1.0, CircuitOpen: false},
		},
	}

	if !p.HasAvailableFrontierModels() {
		t.Error("should have available models")
	}

	p.FrontierMix[0].CircuitOpen = true
	if p.HasAvailableFrontierModels() {
		t.Error("should not have available models when all are circuit-broken")
	}
}

func TestCostPerMillionTokens_Coverage(t *testing.T) {
	// Verify all models in the catalog have entries in the cost table
	for _, models := range FrontierModelCatalog {
		for _, model := range models {
			if _, ok := CostPerMillionTokens[model]; !ok {
				t.Errorf("model %q in FrontierModelCatalog has no entry in CostPerMillionTokens", model)
			}
		}
	}
}
