package main

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

// Preserve source bytes independently of rmapi's mutable incremental mirror.
// The archive is bounded and has no automatic garbage collection: when full,
// stop safely rather than deleting originals or consuming unbounded storage.
func preserveOriginalBundles(mirrorRoot, archiveRoot string) error {
	if err := safeMkdir(archiveRoot, 0700); err != nil {
		return err
	}
	if err := os.Chmod(archiveRoot, 0700); err != nil {
		return err
	}
	entries, err := os.ReadDir(archiveRoot)
	if err != nil {
		return err
	}
	if len(entries) > 20064 {
		return errors.New("original bundle archive entry limit")
	}
	// Atomic source-retention staging is never an original. A process death
	// before rename can leave this private temporary copy; remove only that
	// reserved regular-file pattern before enforcing the immutable archive set.
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".atomic-") {
			path := filepath.Join(archiveRoot, entry.Name())
			if _, err := readRegular(path, maxBundle); err != nil {
				return err
			}
			if err := os.Remove(path); err != nil {
				return err
			}
		}
	}
	if err := syncDir(archiveRoot); err != nil {
		return err
	}
	entries, err = os.ReadDir(archiveRoot)
	if err != nil {
		return err
	}
	if len(entries) > 20000 {
		return errors.New("original bundle archive entry limit")
	}

	var total int64
	known := map[string]bool{}
	for _, entry := range entries {
		name := entry.Name()
		hash := strings.TrimSuffix(name, ".rmdoc")
		if entry.IsDir() || name != hash+".rmdoc" || !validHash(hash) {
			return fmt.Errorf("unexpected original archive entry: %s", name)
		}
		blob, err := readRegular(filepath.Join(archiveRoot, name), maxBundle)
		if err != nil {
			return err
		}
		if digest(blob) != hash {
			return errors.New("original bundle archive modification detected")
		}
		total += int64(len(blob))
		if total > 512<<20 {
			return errors.New("original bundle archive byte limit")
		}
		if err := os.Chmod(filepath.Join(archiveRoot, name), 0400); err != nil {
			return err
		}
		if err := syncRegularFile(filepath.Join(archiveRoot, name)); err != nil {
			return err
		}
		known[hash] = true
	}
	return filepath.WalkDir(mirrorRoot, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || strings.ToLower(filepath.Ext(path)) != ".rmdoc" {
			return nil
		}
		blob, err := readRegular(path, maxBundle)
		if err != nil {
			return err
		}
		hash := digest(blob)
		if known[hash] {
			return nil
		}
		if len(known) >= 20000 || int64(len(blob)) > 512<<20-total {
			return errors.New("original bundle retention budget exhausted; preserve/archive old originals before retrying")
		}
		var stat syscall.Statfs_t
		if err := syscall.Statfs(archiveRoot, &stat); err != nil {
			return err
		}
		free := stat.Bavail * uint64(stat.Bsize)
		if free < uint64(len(blob))+(256<<20) {
			return errors.New("insufficient free-space reserve for original bundle retention")
		}
		if err := atomicWrite(filepath.Join(archiveRoot, hash+".rmdoc"), blob, 0400); err != nil {
			return err
		}
		total += int64(len(blob))
		known[hash] = true
		return nil
	})
}

// The pinned downloader uses os.Create and does not chmod targets. While it
// runs, read-only archive entries/directories protect retained originals from
// its unsanitized remote display-name paths under the required nonroot UID.
// This is not a sandbox against an arbitrary hostile executable of that UID.
func acquireWithProtectedOriginals(archiveRoot string, acquire func() error) (result error) {
	if err := os.Chmod(archiveRoot, 0500); err != nil {
		return err
	}
	defer func() { result = errors.Join(result, os.Chmod(archiveRoot, 0700), syncDir(archiveRoot)) }()
	if err := syncDir(archiveRoot); err != nil {
		return err
	}
	return acquire()
}
