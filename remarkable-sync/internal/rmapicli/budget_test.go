package rmapicli

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestCumulativeBudgetMonitorsPrivateTempAndCache(t *testing.T) {
	for _, where := range []string{"TMPDIR", "XDG_CACHE_HOME"} {
		t.Run(where, func(t *testing.T) {
			fakeDownloader(t, "target=\"$"+where+"/growth\"\ndd if=/dev/zero of=\"$target\" bs=1024 count=512 2>/dev/null\nsleep 30\n")
			policy := acquisitionPolicy{FileBytes: 1 << 20, WorkspaceBytes: 128 << 10, Entries: 100, ReserveBytes: 1, Poll: time.Millisecond}
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			start := time.Now()
			err := runLimited(ctx, t.TempDir(), policy, "-ni", "mget")
			if err == nil {
				t.Fatal("cumulative workspace growth accepted")
			}
			if time.Since(start) > time.Second {
				t.Fatal("growth monitor did not promptly abort")
			}
		})
	}
}
func TestFreeSpaceGateRejectsBeforeSpawn(t *testing.T) {
	root := t.TempDir()
	marker := filepath.Join(root, "spawned")
	fakeDownloader(t, "touch \""+marker+"\"\n")
	policy := defaultPolicy
	free, err := availableBytes(root)
	if err != nil {
		t.Fatal(err)
	}
	policy.ReserveBytes = free + 1
	if err := runLimited(context.Background(), root, policy, "-ni", "ls"); err == nil {
		t.Fatal("free-space gate bypassed")
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatal("downloader spawned before free-space gate")
	}
}
func TestAcquisitionChildEnvironmentIsScoped(t *testing.T) {
	t.Setenv("HA_OCR_TOKEN", "must-not-inherit")
	fakeDownloader(t, "if [ -n \"$HA_OCR_TOKEN\" ]; then exit 9; fi\n[ -n \"$TMPDIR\" ] && [ -n \"$XDG_CACHE_HOME\" ]\n")
	if err := runLimited(context.Background(), t.TempDir(), defaultPolicy, "-ni", "ls"); err != nil {
		t.Fatal(err)
	}
}
