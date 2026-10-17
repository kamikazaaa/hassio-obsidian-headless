package rmapicli

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func fakeDownloader(t *testing.T, script string) {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "rmapi"), []byte("#!/bin/sh\n"+script), 0755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+":"+os.Getenv("PATH"))
}

const parseOutput = `out=""
previous=""
for arg in "$@"; do
 if [ "$previous" = "-o" ]; then out="$arg"; fi
 previous="$arg"
done
`

func TestPublicDownloaderHardFileLimit(t *testing.T) {
	fakeDownloader(t, parseOutput+`dd if=/dev/zero of="$out/too-large.rmdoc" bs=1048576 count=70 2>/dev/null || exit 1
`)
	root := t.TempDir()
	output := filepath.Join(root, "mirror")
	os.Mkdir(output, 0700)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	err := SyncTree(ctx, "/", output)
	if err == nil {
		t.Error("oversized downloader succeeded")
	}
	if info, statErr := os.Stat(filepath.Join(output, "too-large.rmdoc")); statErr == nil && info.Size() > 64<<20 {
		t.Fatalf("child wrote past hard per-file limit: %d", info.Size())
	}
}
func TestPublicDownloaderTimeoutStopsDescendants(t *testing.T) {
	fakeDownloader(t, parseOutput+`(trap "" HUP; sleep 0.3; echo alive > "$out/survived") &
echo "$!" > "$out/child.pid"
wait
`)
	root := t.TempDir()
	output := filepath.Join(root, "mirror")
	os.Mkdir(output, 0700)
	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()
	if err := SyncTree(ctx, "/", output); err == nil {
		t.Fatal("timeout ignored")
	}
	if ctx.Err() != context.DeadlineExceeded {
		t.Fatal("fixture did not reach downloader timeout")
	}
	if _, err := os.Stat(filepath.Join(output, "child.pid")); err != nil {
		t.Fatal("fixture did not start descendant", err)
	}
	time.Sleep(250 * time.Millisecond)
	if _, err := os.Stat(filepath.Join(output, "survived")); err == nil {
		t.Fatal("downloader descendant survives timeout and writes afterward")
	}
}
