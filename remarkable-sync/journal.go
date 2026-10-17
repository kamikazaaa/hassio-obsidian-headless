package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
)

const journalName = ".conversion-transaction"
const maxTransactionBytes = 512 << 20
const maxTransactionEntries = 30002

type journalEntry struct {
	Kind     string `json:"kind"`
	Relative string `json:"relative"`
	Existed  bool   `json:"existed"`
	OldHash  string `json:"old_hash,omitempty"`
	NewHash  string `json:"new_hash,omitempty"`
	Mode     uint32 `json:"mode"`
}
type journalManifest struct {
	Version   int            `json:"version"`
	Committed bool           `json:"committed"`
	Entries   []journalEntry `json:"entries"`
}

var stateNamePattern = regexp.MustCompile(`^(?:[a-f0-9]{64}|collection)\.json$`)

func journalTarget(e journalEntry, stateRoot, destRoot string) (string, error) {
	if err := validRelative(e.Relative, false); err != nil {
		return "", err
	}
	if e.Kind == "state" && stateNamePattern.MatchString(e.Relative) && e.Mode == 0600 {
		return filepath.Join(stateRoot, e.Relative), nil
	}
	if e.Kind == "output" && e.Mode == 0640 {
		return filepath.Join(destRoot, e.Relative), nil
	}
	return "", errors.New("invalid journal target kind, path or mode")
}
func backupPath(root string, i int) string { return filepath.Join(root, fmt.Sprintf("backup-%06d", i)) }
func currentHash(path string) (string, error) {
	data, err := readRegular(path, maxExpanded)
	if os.IsNotExist(err) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	return digest(data), nil
}
func validHash(s string) bool {
	if len(s) != 64 {
		return false
	}
	for _, c := range s {
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
			return false
		}
	}
	return true
}

// Recovery first validates every target and backup, so an unrelated annotation
// prevents all recovery writes. Each rollback step is itself atomic/durable and
// can be repeated after another crash. A committed transaction only needs cleanup.
func recoverTransaction(stateRoot, destRoot string) error {
	return recoverTransactionWithOps(stateRoot, destRoot, syncRegularFile, syncDir)
}

func recoverTransactionWithOps(stateRoot, destRoot string, flushFile func(string) error, flushDir func(string) error) error {
	root := filepath.Join(stateRoot, journalName)
	if _, err := os.Lstat(root); os.IsNotExist(err) {
		return nil
	} else if err != nil {
		return err
	}
	if err := inspectPath(root); err != nil {
		return err
	}
	raw, err := readRegular(filepath.Join(root, "manifest.json"), 16<<20)
	if os.IsNotExist(err) {
		return cleanJournal(root)
	}
	if err != nil {
		return err
	}
	var manifest journalManifest
	if err := json.Unmarshal(raw, &manifest); err != nil {
		return fmt.Errorf("transaction manifest: %w", err)
	}
	if manifest.Version != 1 || len(manifest.Entries) > maxTransactionEntries {
		return errors.New("invalid transaction version or entry count")
	}
	total := int64(0)
	seen := map[string]bool{}
	for i, e := range manifest.Entries {
		target, err := journalTarget(e, stateRoot, destRoot)
		if err != nil {
			return err
		}
		if seen[target] {
			return errors.New("duplicate journal target")
		}
		seen[target] = true
		if (e.Existed && !validHash(e.OldHash)) || (!e.Existed && e.OldHash != "") || (e.NewHash != "" && !validHash(e.NewHash)) {
			return errors.New("invalid journal hashes")
		}
		actual, err := currentHash(target)
		if err != nil {
			return err
		}
		if manifest.Committed {
			if actual != e.NewHash {
				return fmt.Errorf("manual output modification after commit: %s", e.Relative)
			}
		} else if actual != e.OldHash && actual != e.NewHash {
			return fmt.Errorf("manual modification conflicts with transaction recovery: %s", e.Relative)
		}
		if e.Existed {
			backup, err := readRegular(backupPath(root, i), maxExpanded)
			if err != nil {
				return err
			}
			total += int64(len(backup))
			if total > maxTransactionBytes || digest(backup) != e.OldHash {
				return errors.New("invalid or oversized transaction backup")
			}
		}
	}
	if !manifest.Committed {
		for i := len(manifest.Entries) - 1; i >= 0; i-- {
			e := manifest.Entries[i]
			target, _ := journalTarget(e, stateRoot, destRoot)
			actual, err := currentHash(target)
			if err != nil {
				return err
			}
			if actual == e.OldHash {
				continue
			}
			if actual != e.NewHash {
				return fmt.Errorf("target changed during recovery: %s", e.Relative)
			}
			if e.Existed {
				old, err := readRegular(backupPath(root, i), maxExpanded)
				if err != nil {
					return err
				}
				if err := atomicWriteStaged(target, old, os.FileMode(e.Mode), root); err != nil {
					return err
				}
			} else {
				if err := os.Remove(target); err != nil && !os.IsNotExist(err) {
					return err
				}
				if err := flushDir(filepath.Dir(target)); err != nil {
					return err
				}
			}
		}
	}
	// A previous rollback may have restored the old hash and then failed
	// fsync. Hash equality alone does not prove the restoration is durable.
	// Resync every final regular file and its nearest existing parent even
	// when this invocation did not need to change any bytes.
	for _, e := range manifest.Entries {
		target, _ := journalTarget(e, stateRoot, destRoot)
		expected := e.OldHash
		if manifest.Committed {
			expected = e.NewHash
		}
		actual, err := currentHash(target)
		if err != nil {
			return err
		}
		if actual != expected {
			return fmt.Errorf("target changed before recovery durability barrier: %s", e.Relative)
		}
		if expected != "" {
			if err := flushFile(target); err != nil {
				return err
			}
		}
		parent, err := existingParentDirectory(target)
		if err != nil {
			return err
		}
		if err := flushDir(parent); err != nil {
			return err
		}
	}
	return cleanJournal(root)
}

// A not-yet-applied new target can have a parent directory that never existed.
// Sync the nearest existing ancestor rather than requiring an absent directory.
func existingParentDirectory(target string) (string, error) {
	for parent := filepath.Dir(target); ; parent = filepath.Dir(parent) {
		if err := inspectPath(parent); err != nil {
			return "", err
		}
		info, err := os.Lstat(parent)
		if err == nil {
			if !info.IsDir() {
				return "", fmt.Errorf("recovery parent is not a directory: %s", parent)
			}
			return parent, nil
		}
		if !os.IsNotExist(err) {
			return "", err
		}
		if filepath.Dir(parent) == parent {
			return "", err
		}
	}
}

// Only our fixed-format private journal files are removed. Unknown material,
// directories, links or special files are preserved as explicit conflicts.
func cleanJournal(root string) error {
	entries, err := os.ReadDir(root)
	if err != nil {
		return err
	}
	if len(entries) > maxTransactionEntries+10 {
		return errors.New("journal cleanup count limit")
	}
	for _, e := range entries {
		name := e.Name()
		allowed := name == "manifest.json" || regexp.MustCompile(`^backup-[0-9]{6}$`).MatchString(name) || strings.HasPrefix(name, ".atomic-") || strings.HasPrefix(name, ".conversion-")
		if !allowed || e.IsDir() {
			return fmt.Errorf("unexpected private journal entry: %s", name)
		}
		if err := inspectPath(filepath.Join(root, name)); err != nil {
			return err
		}
	}
	// Once recovery/commit is complete, clear the manifest durably before backups.
	// A subsequent cleanup can then finish without requiring already removed backups.
	manifest := filepath.Join(root, "manifest.json")
	if err := os.Remove(manifest); err != nil && !os.IsNotExist(err) {
		return err
	}
	if err := syncDir(root); err != nil {
		return err
	}
	for _, e := range entries {
		if e.Name() == "manifest.json" {
			continue
		}
		if err := os.Remove(filepath.Join(root, e.Name())); err != nil {
			return err
		}
	}
	if err := os.Remove(root); err != nil {
		return err
	}
	return syncDir(filepath.Dir(root))
}

func prepareTransaction(stateRoot, destRoot string, changes []mutation) (journalManifest, error) {
	var manifest journalManifest
	manifest.Version = 1
	if len(changes) > maxTransactionEntries {
		return manifest, errors.New("transaction entry count limit")
	}
	root := filepath.Join(stateRoot, journalName)
	if _, err := os.Lstat(root); err == nil {
		return manifest, errors.New("unrecovered transaction exists")
	} else if !os.IsNotExist(err) {
		return manifest, err
	}
	if err := safeMkdir(root, 0700); err != nil {
		return manifest, err
	}
	total := int64(0)
	seen := map[string]bool{}
	for i, m := range changes {
		e := journalEntry{Mode: uint32(m.mode)}
		absolute, err := filepath.Abs(m.path)
		if err != nil {
			return manifest, err
		}
		for _, candidate := range []struct{ kind, root string }{{"output", destRoot}, {"state", stateRoot}} {
			base, err := filepath.Abs(candidate.root)
			if err != nil {
				return manifest, err
			}
			rel, err := filepath.Rel(base, absolute)
			if err != nil {
				return manifest, err
			}
			if rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) && rel != "." {
				e.Kind = candidate.kind
				e.Relative = rel
				break
			}
		}
		if _, err := journalTarget(e, stateRoot, destRoot); err != nil {
			return manifest, err
		}
		for path := range seen {
			if path == absolute || strings.HasPrefix(path, absolute+string(filepath.Separator)) || strings.HasPrefix(absolute, path+string(filepath.Separator)) {
				return manifest, errors.New("colliding transaction targets")
			}
		}
		seen[absolute] = true
		previous, err := readRegular(m.path, maxExpanded)
		if err == nil {
			e.Existed = true
			e.OldHash = digest(previous)
			total += int64(len(previous))
			if total > maxTransactionBytes {
				return manifest, errors.New("transaction backup size limit")
			}
			if err := atomicWrite(backupPath(root, i), previous, 0600); err != nil {
				return manifest, err
			}
		} else if !os.IsNotExist(err) {
			return manifest, err
		}
		if m.checkExpected && e.OldHash != m.expected {
			return manifest, fmt.Errorf("output changed before journaling: %s", m.path)
		}
		if !m.remove {
			e.NewHash = digest(m.data)
		}
		manifest.Entries = append(manifest.Entries, e)
	}
	raw, err := json.Marshal(manifest)
	if err != nil {
		return manifest, err
	}
	if len(raw) > 16<<20 {
		return manifest, errors.New("transaction manifest size limit")
	}
	if err := atomicWrite(filepath.Join(root, "manifest.json"), raw, 0600); err != nil {
		return manifest, err
	}
	if err := syncDir(stateRoot); err != nil {
		return manifest, err
	}
	return manifest, nil
}

func applyTransaction(stateRoot, destRoot string, changes []mutation) error {
	if len(changes) == 0 {
		return nil
	}
	manifest, err := prepareTransaction(stateRoot, destRoot, changes)
	if err != nil {
		return err
	}
	for i := range changes {
		changes[i].expected = manifest.Entries[i].OldHash
		changes[i].checkExpected = true
	}
	if err := applyChangesStaged(changes, os.Rename, syncDir, filepath.Join(stateRoot, journalName)); err != nil {
		return errors.Join(err, recoverTransaction(stateRoot, destRoot))
	}
	manifest.Committed = true
	raw, err := json.Marshal(manifest)
	if err != nil {
		return err
	}
	if err := atomicWrite(filepath.Join(stateRoot, journalName, "manifest.json"), raw, 0600); err != nil {
		return err
	}
	return cleanJournal(filepath.Join(stateRoot, journalName))
}

// Hold a kernel lock through recovery, acquisition and commit. The lock inode
// remains in private state, so a second process cannot clean an active journal.
func acquireConversionLock(stateRoot string) (*os.File, error) {
	path := filepath.Join(stateRoot, ".conversion.lock")
	if err := inspectPath(path); err != nil {
		return nil, err
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		file.Close()
		return nil, errors.New("another conversion owns the private state lock")
	}
	return file, nil
}

func syncRegularFile(path string) error {
	file, err := openNoFollow(path)
	if err != nil {
		return err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("cannot sync nonregular file: %s", path)
	}
	return file.Sync()
}
