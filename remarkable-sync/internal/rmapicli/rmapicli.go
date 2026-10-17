// Package rmapicli wraps the rmapi CLI binary (github.com/ddvk/rmapi), which
// this add-on already builds into the image. Rather than maintaining our own
// client against reMarkable Cloud's private API — which has repeatedly
// broken as reMarkable moved hosts/protocols — we shell out to rmapi for
// authentication and document sync, the same way this add-on wraps
// obsidian-headless and obsidian-web-mcp instead of reimplementing them.
package rmapicli

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

const binary = "rmapi"

// configPath returns the path rmapi reads/writes its device+user tokens to.
// Mirrors rmapi's own resolution order (RMAPI_CONFIG env var, then ~/.rmapi)
// closely enough for an existence check — this add-on always sets
// RMAPI_CONFIG explicitly (managed runtime), so the fallback rarely matters.
func configPath() string {
	if p := os.Getenv("RMAPI_CONFIG"); p != "" {
		return p
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(home, ".rmapi")
}

// IsAuthenticated reports whether rmapi already has a usable device token.
//
// Checks for the config file first rather than always shelling out: with no
// device token, `rmapi ls` falls into rmapi's interactive one-time-code
// prompt, reading from stdin — which we deliberately don't supply here (it
// defaults to /dev/null) — and depending on how rmapi handles that EOF, the
// process can retry for the full context timeout before giving up. Calling
// this from an HTTP handler (as the old registration UI did, on every page
// load) made the whole page hang for up to 30s while unregistered, which is
// indistinguishable from the server not listening at all.
func HasCredentials() bool {
	info, err := os.Lstat(configPath())
	return err == nil && info.Mode().IsRegular() && info.Mode().Perm()&0077 == 0
}

func IsAuthenticated(ctx context.Context) bool {
	if !HasCredentials() {
		return false
	}

	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	workspace := os.Getenv("REMARKABLE_CACHE_DIR")
	if workspace == "" {
		workspace = filepath.Join(filepath.Dir(configPath()), "private")
	}
	return runLimited(ctx, workspace, defaultPolicy, "-ni", "ls") == nil
}

// SyncTree recursively mirrors remotePath into outputDir as .rmdoc bundles,
// preserving reMarkable's folder hierarchy as real directories. Incremental:
// only files whose remote copy is newer than the local one are re-fetched.
func SyncTree(ctx context.Context, remotePath, outputDir string) error {
	if err := runLimited(ctx, filepath.Dir(outputDir), defaultPolicy, "-ni", "mget", "-i", "-o", outputDir, remotePath); err != nil {
		return fmt.Errorf("rmapi acquisition: %w", err)
	}
	return nil
}
