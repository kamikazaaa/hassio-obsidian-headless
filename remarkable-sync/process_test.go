package main

import (
	"context"
	"flag"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestWorkerProcess(t *testing.T) {
	if os.Getenv("REMARKABLE_TEST_PROCESS") != "true" {
		return
	}
	flag.CommandLine = flag.NewFlagSet("remarkable-sync", flag.ExitOnError)
	os.Args = []string{"remarkable-sync", "-continuous"}
	if os.Getenv("REMARKABLE_TEST_HELP") == "true" {
		os.Args = []string{"remarkable-sync", "-help"}
	}
	main()
	os.Exit(0)
}
func TestOneShotExitStatusAndObsoleteOptions(t *testing.T) {
	for _, kind := range []string{"success", "conversion-failure", "obsolete-option", "help-redaction"} {
		t.Run(kind, func(t *testing.T) {
			installFakeRmapi(t)
			root := t.TempDir()
			private := filepath.Join(root, "private")
			dest := filepath.Join(root, "generated")
			config := filepath.Join(root, "rmapi.json")
			os.WriteFile(config, []byte("dummy credential"), 0600)
			seedRmdoc(t, filepath.Join(private, "mirror", "note.rmdoc"), `{"fileType":"notebook"}`, `{"visibleName":"Notebook"}`)
			if kind == "conversion-failure" {
				os.WriteFile(filepath.Join(private, "mirror", "note.rmdoc"), []byte("broken zip"), 0600)
			}
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestWorkerProcess$")
			cmd.Env = []string{"PATH=" + os.Getenv("PATH"), "REMARKABLE_TEST_PROCESS=true", "SYNC_ONCE=true", "REMARKABLE_SYNC_ENABLED=true", "VAULT_PATH=" + dest, "REMARKABLE_CACHE_DIR=" + private, "RMAPI_CONFIG=" + config, "HA_OCR_TOKEN=must-never-appear", "HA_OCR_URL=secret-url-must-never-appear"}
			// Don't send an OCR request: native pen stroke transcription is unsupported.
			if kind == "help-redaction" {
				cmd.Env = append(cmd.Env, "REMARKABLE_TEST_HELP=true")
			}
			if kind == "obsolete-option" {
				cmd.Env = append(cmd.Env, "REMARKABLE_REGISTER_PORT=8421")
			}
			out, err := cmd.CombinedOutput()
			if ctx.Err() != nil {
				t.Fatal("SYNC_ONCE did not terminate", ctx.Err())
			}
			if kind == "help-redaction" {
				if err != nil {
					t.Fatal(err)
				}
			} else if kind == "success" {
				if err != nil {
					t.Fatalf("one-shot failed: %v %s", err, out)
				}
				body, _ := os.ReadFile(filepath.Join(dest, "documents", "note.md"))
				if !strings.Contains(string(body), "pen-stroke transcription is unsupported") {
					t.Fatal("native OCR support misrepresented")
				}
			} else if err == nil {
				t.Fatal("invalid conversion/obsolete option exited successfully")
			}
			if strings.Contains(string(out), "must-never-appear") {
				t.Fatal("credential printed in diagnostic")
			}
		})
	}
}
