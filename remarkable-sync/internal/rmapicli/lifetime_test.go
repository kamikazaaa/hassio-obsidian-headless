package rmapicli

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

func TestPdeathChildFixture(t *testing.T) {
	if os.Getenv("REMARKABLE_LIFETIME_CHILD") != "1" {
		return
	}
	home := os.Getenv("HOME")
	if err := os.WriteFile(filepath.Join(home, "started"), []byte("started"), 0600); err != nil {
		os.Exit(2)
	}
	time.Sleep(time.Second)
	if err := os.WriteFile(filepath.Join(home, "survived"), []byte("survived"), 0600); err != nil {
		os.Exit(3)
	}
	os.Exit(0)
}
func TestPdeathWorkerFixture(t *testing.T) {
	if os.Getenv("REMARKABLE_LIFETIME_WORKER") != "1" {
		return
	}
	if err := runLimited(context.Background(), os.Getenv("REMARKABLE_LIFETIME_WORKSPACE"), defaultPolicy, "-ni", "ls"); err != nil {
		os.Exit(4)
	}
	os.Exit(0)
}
func TestActualDownloaderDiesWithConverter(t *testing.T) {
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	fakeDownloader(t, fmt.Sprintf("export REMARKABLE_LIFETIME_CHILD=1\nexec %q -test.run=^TestPdeathChildFixture$\n", self))
	workspace := t.TempDir()
	cmd := exec.Command(self, "-test.run=^TestPdeathWorkerFixture$")
	cmd.Env = append(os.Environ(), "REMARKABLE_LIFETIME_WORKER=1", "REMARKABLE_LIFETIME_WORKSPACE="+workspace)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = cmd.Process.Kill(); _ = cmd.Wait() }()
	started := filepath.Join(workspace, "client-home", "started")
	deadline := time.Now().Add(3 * time.Second)
	for {
		if _, err := os.Stat(started); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("downloader fixture did not start")
		}
		time.Sleep(5 * time.Millisecond)
	}
	if err := cmd.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	_ = cmd.Wait()
	time.Sleep(1200 * time.Millisecond)
	if _, err := os.Stat(filepath.Join(workspace, "client-home", "survived")); err == nil {
		t.Fatal("actual downloader survives abrupt converter death")
	}
}
