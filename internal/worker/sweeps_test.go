package worker

import (
	"testing"

	ftypes "github.com/Fremen-Labs/flume/pkg/types"
)

func TestPromoteHierarchySiblings(t *testing.T) {
	// Phase 0 unit test for promote hierarchy siblings (sweeps_test).
	// Exercises the core "org parent stays planned but task siblings promote on depends" semantics
	// (the P0 deadlock from plan-new...md + sweeps.go:420 FIX). Uses the extracted pure hook for
	// isolation (no ES, no side effects; reliable-go: table tests, small units, verifiability).
	// Covers: planned structural parent ok, blocked parent blocks, unmet dep blocks, depth guard.
	cases := []struct {
		name       string
		pt         plannedTask
		cache      map[string]string
		wantOK     bool
		wantReason string
	}{
		{
			name: "sibling under planned org parent promotes once depends met (core anti-deadlock)",
			pt: plannedTask{
				ID:             "task-sib-2",
				ParentID:       "story-42",
				DependsOn:      []string{"task-sib-1"},
				HierarchyDepth: 3,
				PlanSessionID:  "ps-abc123",
			},
			cache:  map[string]string{"story-42": "planned", "task-sib-1": "done"},
			wantOK: true,
		},
		{
			name: "first sibling also promotes (no parent dep)",
			pt: plannedTask{
				ID:             "task-sib-1",
				ParentID:       "story-42",
				DependsOn:      []string{},
				HierarchyDepth: 3,
				PlanSessionID:  "ps-abc123",
			},
			cache:  map[string]string{"story-42": "planned"},
			wantOK: true,
		},
		{
			name: "org parent blocked prevents promotion (safety)",
			pt: plannedTask{
				ID:             "task-under-blocked",
				ParentID:       "story-blocked",
				DependsOn:      []string{},
				HierarchyDepth: 3,
			},
			cache:      map[string]string{"story-blocked": "blocked"},
			wantOK:     false,
			wantReason: "parent_inactive",
		},
		{
			name: "depends unmet blocks sibling (ordering)",
			pt: plannedTask{
				ID:             "task-waiting",
				ParentID:       "story-42",
				DependsOn:      []string{"task-sib-1"},
				HierarchyDepth: 3,
			},
			cache:      map[string]string{"story-42": "planned", "task-sib-1": "planned"},
			wantOK:     false,
			wantReason: "dep_unmet",
		},
		{
			name: "depth exceeded always blocks (anti-explosion, even if deps met)",
			pt: plannedTask{
				ID:             "deep-task",
				ParentID:       "",
				DependsOn:      []string{},
				HierarchyDepth: ftypes.MAX_HIERARCHY_DEPTH + 5,
			},
			cache:      map[string]string{},
			wantOK:     false,
			wantReason: "depth_exceeded",
		},
		{
			name: "archived parent allows? no, inactive",
			pt: plannedTask{
				ID:             "t-arch",
				ParentID:       "epic-arch",
				DependsOn:      []string{},
				HierarchyDepth: 1,
			},
			cache:      map[string]string{"epic-arch": "archived"},
			wantOK:     false,
			wantReason: "parent_inactive",
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			ok, reason := canPromoteSiblingsTestHook(c.pt, c.cache)
			if ok != c.wantOK {
				t.Errorf("ok=%v want=%v (reason=%q)", ok, c.wantOK, reason)
			}
			if c.wantReason != "" && reason != c.wantReason {
				t.Errorf("reason=%q want=%q", reason, c.wantReason)
			}
		})
	}
}