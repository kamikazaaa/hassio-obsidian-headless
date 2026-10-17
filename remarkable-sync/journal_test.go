package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

func interruptedFixture(t *testing.T) (string, string, string, []mutation) {
	t.Helper()
	mirror, state, dest := fixture(t)
	input := filepath.Join(mirror, "index.rmdoc")
	if _, _, err := syncDocument(input, "index", state, dest, nil, "", nil); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(input, bundle(t, map[string]string{"d.content": `{"fileType":"pdf"}`, "d.metadata": `{"visibleName":"Updated"}`, "d.pdf": "%PDF-new"}), 0600)
	p, err := planDocument(input, "index", state, dest, nil, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	return input, state, dest, p.changes
}
func TestJournalRecoversEveryCommitBoundary(t *testing.T) {
	// One old note, one newly created PDF, and the private conversion state.
	for boundary := 0; boundary <= 3; boundary++ {
		t.Run(fmt.Sprint(boundary), func(t *testing.T) {
			input, state, dest, changes := interruptedFixture(t)
			before, _ := os.ReadFile(filepath.Join(dest, "index.md"))
			oldState, _ := os.ReadFile(statePath(state, "index"))
			if _, err := prepareTransaction(state, dest, changes); err != nil {
				t.Fatal(err)
			}
			partial := append([]mutation(nil), changes[:boundary]...)
			if err := applyChanges(partial); err != nil {
				t.Fatal(err)
			}
			// Reconstruct startup after a process death without relying on in-memory plan.
			if err := recoverTransaction(state, dest); err != nil {
				t.Fatal(err)
			}
			after, _ := os.ReadFile(filepath.Join(dest, "index.md"))
			saved, _ := os.ReadFile(statePath(state, "index"))
			if !bytes.Equal(before, after) || !bytes.Equal(oldState, saved) {
				t.Fatal("rollback did not restore last good bytes/state")
			}
			if _, err := os.Stat(filepath.Join(dest, "index.pdf")); !os.IsNotExist(err) {
				t.Fatal("new PDF survived rollback")
			}
			if _, _, err := syncDocument(input, "index", state, dest, nil, "", nil); err != nil {
				t.Fatal("automatic retry failed", err)
			}
			note, _ := os.ReadFile(filepath.Join(dest, "index.md"))
			if !bytes.Contains(note, []byte("Updated")) {
				t.Fatal("retry did not publish current conversion")
			}
			if _, err := os.Stat(filepath.Join(state, journalName)); !os.IsNotExist(err) {
				t.Fatal("journal not removed")
			}
		})
	}
}
func TestJournalPreservesUnexpectedManualChanges(t *testing.T) {
	for boundary := 0; boundary <= 3; boundary++ {
		t.Run(fmt.Sprint(boundary), func(t *testing.T) {
			_, state, dest, changes := interruptedFixture(t)
			if _, err := prepareTransaction(state, dest, changes); err != nil {
				t.Fatal(err)
			}
			if err := applyChanges(append([]mutation(nil), changes[:boundary]...)); err != nil {
				t.Fatal(err)
			}
			annotation := []byte("new private annotation")
			os.WriteFile(filepath.Join(dest, "index.md"), annotation, 0640)
			priorState, _ := os.ReadFile(statePath(state, "index"))
			if err := recoverTransaction(state, dest); err == nil {
				t.Fatal("manual modification did not block recovery")
			}
			got, _ := os.ReadFile(filepath.Join(dest, "index.md"))
			saved, _ := os.ReadFile(statePath(state, "index"))
			if !bytes.Equal(got, annotation) || !bytes.Equal(saved, priorState) {
				t.Fatal("recovery changed data before validating all targets")
			}
		})
	}
}
func TestJournalRecoversInterruptedRollbackAndCommittedCleanup(t *testing.T) {
	for _, committed := range []bool{false, true} {
		t.Run(fmt.Sprint(committed), func(t *testing.T) {
			input, state, dest, changes := interruptedFixture(t)
			manifest, err := prepareTransaction(state, dest, changes)
			if err != nil {
				t.Fatal(err)
			}
			if err := applyChanges(changes); err != nil {
				t.Fatal(err)
			}
			if committed {
				manifest.Committed = true
				raw, _ := json.Marshal(manifest)
				if err := atomicWrite(filepath.Join(state, journalName, "manifest.json"), raw, 0600); err != nil {
					t.Fatal(err)
				}
			} else {
				// A previous recovery restored one target and died; the rest remain new.
				for i, e := range manifest.Entries {
					if e.Existed && e.Kind == "output" {
						old, _ := os.ReadFile(backupPath(filepath.Join(state, journalName), i))
						target, _ := journalTarget(e, state, dest)
						if err := atomicWrite(target, old, 0640); err != nil {
							t.Fatal(err)
						}
						break
					}
				}
			}
			if err := recoverTransaction(state, dest); err != nil {
				t.Fatal(err)
			}
			changed, _, err := syncDocument(input, "index", state, dest, nil, "", nil)
			if err != nil {
				t.Fatal(err)
			}
			if changed == committed {
				t.Fatalf("wrong retry/cache result: committed=%v changed=%v", committed, changed)
			}
		})
	}
}
func TestJournalIncompletePreparationAndUnsafeManifest(t *testing.T) {
	_, state, dest, changes := interruptedFixture(t)
	root := filepath.Join(state, journalName)
	os.MkdirAll(root, 0700)
	os.WriteFile(backupPath(root, 0), []byte("incomplete preparation"), 0600)
	if err := recoverTransaction(state, dest); err != nil {
		t.Fatal(err)
	}
	manifest, err := prepareTransaction(state, dest, changes)
	if err != nil {
		t.Fatal(err)
	}
	outside := filepath.Join(t.TempDir(), "outside")
	os.WriteFile(outside, []byte("keep"), 0600)
	manifest.Entries[0].Relative = "../outside"
	raw, _ := json.Marshal(manifest)
	os.WriteFile(filepath.Join(root, "manifest.json"), raw, 0600)
	if err := recoverTransaction(state, dest); err == nil {
		t.Fatal("escaping manifest accepted")
	}
	got, _ := os.ReadFile(outside)
	if string(got) != "keep" {
		t.Fatal("outside file changed")
	}
}

func TestJournalPrivateStagingCleanupAndRetry(t *testing.T) {
	input, state, dest, changes := interruptedFixture(t)
	if _, err := prepareTransaction(state, dest, changes); err != nil {
		t.Fatal(err)
	}
	// A crash during staging or during rollback leaves temporary files only in
	// the private journal, never in the generated publication candidate tree.
	root := filepath.Join(state, journalName)
	os.WriteFile(filepath.Join(root, ".conversion-interrupted"), []byte("partial new file"), 0600)
	os.WriteFile(filepath.Join(root, ".atomic-interrupted"), []byte("partial rollback"), 0600)
	if err := recoverTransaction(state, dest); err != nil {
		t.Fatal(err)
	}
	if _, _, err := syncDocument(input, "index", state, dest, nil, "", nil); err != nil {
		t.Fatal(err)
	}
}

func TestJournalRestoresRemovedPDFAndRetries(t *testing.T) {
	for boundary := 0; boundary <= 3; boundary++ {
		t.Run(fmt.Sprint(boundary), func(t *testing.T) {
			input, state, dest, _ := interruptedFixture(t)
			if _, _, err := syncDocument(input, "index", state, dest, nil, "", nil); err != nil {
				t.Fatal(err)
			}
			pdf := filepath.Join(dest, "index.pdf")
			before, _ := os.ReadFile(pdf)
			os.WriteFile(input, bundle(t, map[string]string{"d.content": `{"fileType":"notebook"}`}), 0600)
			p, err := planDocument(input, "index", state, dest, nil, "", nil)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := prepareTransaction(state, dest, p.changes); err != nil {
				t.Fatal(err)
			}
			if err := applyChanges(append([]mutation(nil), p.changes[:boundary]...)); err != nil {
				t.Fatal(err)
			}
			if err := recoverTransaction(state, dest); err != nil {
				t.Fatal(err)
			}
			after, _ := os.ReadFile(pdf)
			if !bytes.Equal(before, after) {
				t.Fatal("removed PDF not restored")
			}
			if _, _, err := syncDocument(input, "index", state, dest, nil, "", nil); err != nil {
				t.Fatal(err)
			}
			if _, err := os.Stat(pdf); !os.IsNotExist(err) {
				t.Fatal("obsolete PDF not removed on successful retry")
			}
		})
	}
}

func TestConversionLockExcludesOverlappingWorkers(t *testing.T) {
	root := t.TempDir()
	first, err := acquireConversionLock(root)
	if err != nil {
		t.Fatal(err)
	}
	if second, err := acquireConversionLock(root); err == nil {
		second.Close()
		first.Close()
		t.Fatal("concurrent converter acquired active state")
	}
	first.Close()
	third, err := acquireConversionLock(root)
	if err != nil {
		t.Fatal(err)
	}
	third.Close()
}

func TestRollbackDeletionPropagatesParentSyncFailure(t *testing.T) {
	root := t.TempDir()
	note := filepath.Join(root, "new.md")
	state := filepath.Join(root, "state.json")
	os.WriteFile(state, []byte("old"), 0600)
	changes := []mutation{{path: note, data: []byte("new"), mode: 0640}, {path: state, data: []byte("new state"), mode: 0600}}
	calls := 0
	rename := func(a, b string) error {
		calls++
		if calls == 2 {
			return syscall.ENOSPC
		}
		return os.Rename(a, b)
	}
	flush := func(path string) error {
		if _, err := os.Stat(note); os.IsNotExist(err) {
			return syscall.EIO
		}
		return syncDir(path)
	}
	err := applyChangesWithOps(changes, rename, flush)
	if !errors.Is(err, syscall.EIO) {
		t.Fatalf("rollback deletion parent fsync failure lost: %v", err)
	}
	if _, err := os.Stat(note); !os.IsNotExist(err) {
		t.Fatal("new target not removed")
	}
}

func TestRecoveryRetriesSyncOfAlreadyRestoredTargets(t *testing.T) {
	for _, kind := range []string{"absent-new-target", "existing-old-target"} {
		t.Run(kind, func(t *testing.T) {
			root := t.TempDir()
			state := filepath.Join(root, "state")
			dest := filepath.Join(root, "generated")
			safeMkdir(state, 0700)
			safeMkdir(dest, 0700)
			target := filepath.Join(dest, "note.md")
			if kind == "existing-old-target" {
				os.WriteFile(target, []byte("old bytes"), 0640)
			}
			changes := []mutation{{path: target, data: []byte("new bytes"), mode: 0640}}
			if _, err := prepareTransaction(state, dest, changes); err != nil {
				t.Fatal(err)
			}
			fail := func(string) error { return syscall.EIO }
			flushFile := syncRegularFile
			flushDir := syncDir
			if kind == "existing-old-target" {
				flushFile = fail
			} else {
				flushDir = fail
			}
			if err := recoverTransactionWithOps(state, dest, flushFile, flushDir); !errors.Is(err, syscall.EIO) {
				t.Fatalf("already-old target bypassed required fsync: %v", err)
			}
			if _, err := os.Stat(filepath.Join(state, journalName, "manifest.json")); err != nil {
				t.Fatal("journal discarded before durable recovery", err)
			}
			fileSynced := false
			parentSynced := false
			flushFile = func(path string) error { fileSynced = true; return syncRegularFile(path) }
			flushDir = func(path string) error { parentSynced = true; return syncDir(path) }
			if err := recoverTransactionWithOps(state, dest, flushFile, flushDir); err != nil {
				t.Fatal("recovery retry failed", err)
			}
			if !parentSynced || (kind == "existing-old-target" && !fileSynced) {
				t.Fatal("retry skipped file or parent durability barrier")
			}
			if _, err := os.Stat(filepath.Join(state, journalName)); !os.IsNotExist(err) {
				t.Fatal("successful durable retry retained journal")
			}
		})
	}
}

func TestRecoveryDeletionSyncFailureRetainsJournalAcrossRetry(t *testing.T) {
	root := t.TempDir()
	state := filepath.Join(root, "state")
	dest := filepath.Join(root, "generated")
	safeMkdir(state, 0700)
	safeMkdir(dest, 0700)
	target := filepath.Join(dest, "new.md")
	changes := []mutation{{path: target, data: []byte("new bytes"), mode: 0640}}
	if _, err := prepareTransaction(state, dest, changes); err != nil {
		t.Fatal(err)
	}
	if err := applyChanges(changes); err != nil {
		t.Fatal(err)
	}
	fail := func(string) error { return syscall.EIO }
	for attempt := 0; attempt < 2; attempt++ {
		// The first call removes the new file and fails its parent fsync. The
		// second sees the old absent hash but must retry the durability barrier.
		if err := recoverTransactionWithOps(state, dest, syncRegularFile, fail); !errors.Is(err, syscall.EIO) {
			t.Fatalf("attempt %d hid parent fsync error: %v", attempt, err)
		}
		if _, err := os.Stat(target); !os.IsNotExist(err) {
			t.Fatal("new file not rolled back")
		}
		if _, err := os.Stat(filepath.Join(state, journalName, "manifest.json")); err != nil {
			t.Fatal("journal cleared before durable deletion", err)
		}
	}
	if err := recoverTransaction(state, dest); err != nil {
		t.Fatal("durable retry failed", err)
	}
}

func TestRecoverySyncsNearestExistingParentForUnappliedNestedTarget(t *testing.T) {
	root := t.TempDir()
	state := filepath.Join(root, "state")
	dest := filepath.Join(root, "generated")
	safeMkdir(state, 0700)
	safeMkdir(dest, 0700)
	target := filepath.Join(dest, "never-created", "nested", "note.md")
	if _, err := prepareTransaction(state, dest, []mutation{{path: target, data: []byte("new"), mode: 0640}}); err != nil {
		t.Fatal(err)
	}
	synced := ""
	flush := func(path string) error { synced = path; return syncDir(path) }
	if err := recoverTransactionWithOps(state, dest, syncRegularFile, flush); err != nil {
		t.Fatal(err)
	}
	if synced != dest {
		t.Fatalf("synced %q, want existing generated root %q", synced, dest)
	}
}

func TestRecoveryRestorationSyncFailureRetriesBeforeCleanup(t *testing.T) {
	root := t.TempDir()
	state := filepath.Join(root, "state")
	dest := filepath.Join(root, "generated")
	safeMkdir(state, 0700)
	safeMkdir(dest, 0700)
	target := filepath.Join(dest, "note.md")
	os.WriteFile(target, []byte("old bytes"), 0640)
	changes := []mutation{{path: target, data: []byte("new bytes"), mode: 0640}}
	if _, err := prepareTransaction(state, dest, changes); err != nil {
		t.Fatal(err)
	}
	if err := applyChanges(changes); err != nil {
		t.Fatal(err)
	}
	flush := func(path string) error {
		got, _ := os.ReadFile(path)
		if string(got) != "old bytes" {
			t.Fatal("sync preceded restoration")
		}
		return syscall.EIO
	}
	for attempt := 0; attempt < 2; attempt++ {
		if err := recoverTransactionWithOps(state, dest, flush, syncDir); !errors.Is(err, syscall.EIO) {
			t.Fatal("restored-file sync error lost", err)
		}
		if _, err := os.Stat(filepath.Join(state, journalName, "manifest.json")); err != nil {
			t.Fatal("journal discarded after failed restored-file sync", err)
		}
	}
	if err := recoverTransaction(state, dest); err != nil {
		t.Fatal(err)
	}
}
