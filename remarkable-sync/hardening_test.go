package main

import (
	"archive/zip"
	"bytes"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"

	"remarkable-sync/internal/ocr"
)

func fixture(t *testing.T) (string, string, string) {
	t.Helper()
	root := t.TempDir()
	mirror := filepath.Join(root, "mirror")
	state := filepath.Join(root, "state")
	dest := filepath.Join(root, "generated")
	seedRmdoc(t, filepath.Join(mirror, "index.rmdoc"), `{"fileType":"notebook","tags":[{"name":"work"}]}`, `{"visibleName":"Original"}`)
	return mirror, state, dest
}
func bundle(t *testing.T, entries map[string]string) []byte {
	t.Helper()
	var b bytes.Buffer
	w := zip.NewWriter(&b)
	for name, body := range entries {
		f, e := w.Create(name)
		if e != nil {
			t.Fatal(e)
		}
		if _, e = f.Write([]byte(body)); e != nil {
			t.Fatal(e)
		}
	}
	if e := w.Close(); e != nil {
		t.Fatal(e)
	}
	return b.Bytes()
}
func TestHardeningCacheUsesBytesConfigAndOutputs(t *testing.T) {
	mirror, state, dest := fixture(t)
	input := filepath.Join(mirror, "index.rmdoc")
	sync := func(prefix string) bool {
		t.Helper()
		changed, _, err := syncDocument(input, "index", state, dest, nil, prefix, nil)
		if err != nil {
			t.Fatal(err)
		}
		return changed
	}
	if !sync("") {
		t.Fatal("initial sync unchanged")
	}
	if sync("") {
		t.Fatal("cache miss")
	}
	if !sync("rm:") {
		t.Fatal("config change skipped")
	}
	if err := os.Remove(filepath.Join(dest, "index.md")); err != nil {
		t.Fatal(err)
	}
	if !sync("rm:") {
		t.Fatal("missing output skipped")
	}
	info, _ := os.Stat(input)
	seedRmdoc(t, input, `{"fileType":"notebook"}`, `{"visibleName":"New bytes"}`)
	os.Chtimes(input, info.ModTime(), info.ModTime())
	if !sync("rm:") {
		t.Fatal("same-mtime changed source skipped")
	}
	body, _ := os.ReadFile(filepath.Join(dest, "index.md"))
	if !strings.Contains(string(body), "New bytes") {
		t.Fatal("new title missing")
	}
}
func TestHardeningPreservesAnnotations(t *testing.T) {
	mirror, state, dest := fixture(t)
	input := filepath.Join(mirror, "index.rmdoc")
	if _, _, e := syncDocument(input, "index", state, dest, nil, "", nil); e != nil {
		t.Fatal(e)
	}
	note := filepath.Join(dest, "index.md")
	annotation := []byte("my private annotation")
	os.WriteFile(note, annotation, 0600)
	if _, _, e := syncDocument(input, "index", state, dest, nil, "new:", nil); e == nil {
		t.Fatal("manual modification accepted")
	}
	got, _ := os.ReadFile(note)
	if !bytes.Equal(got, annotation) {
		t.Fatal("annotation overwritten")
	}
}
func TestHardeningIndexCollisionAndRepair(t *testing.T) {
	installFakeRmapi(t)
	mirror, state, dest := fixture(t)
	if e := runSync(mirror, state, dest, nil, "", nil); e != nil {
		t.Fatal(e)
	}
	body, _ := os.ReadFile(filepath.Join(dest, "index.md"))
	if !strings.Contains(string(body), "Original") {
		t.Fatal("collection index clobbered document index.md")
	}
	idx := filepath.Join(dest, "_remarkable_collection", "index.md")
	if e := os.Remove(idx); e != nil {
		t.Fatal(e)
	}
	if e := runSync(mirror, state, dest, nil, "", nil); e != nil {
		t.Fatal(e)
	}
	if _, e := os.Stat(idx); e != nil {
		t.Fatal("missing index not repaired", e)
	}
}
func TestHardeningRejectsTraversal(t *testing.T) {
	mirror, state, dest := fixture(t)
	for _, name := range []string{"../escape", "/absolute", "_remarkable_collection/index"} {
		if _, _, e := syncDocument(filepath.Join(mirror, "index.rmdoc"), name, state, dest, nil, "", nil); e == nil {
			t.Fatalf("accepted %q", name)
		}
	}
}
func TestHardeningRequiredPDFWriteFailure(t *testing.T) {
	mirror, state, dest := fixture(t)
	os.MkdirAll(filepath.Join(dest, "index.pdf"), 0700)
	os.WriteFile(filepath.Join(mirror, "index.rmdoc"), bundle(t, map[string]string{"d.content": `{"fileType":"pdf"}`, "d.pdf": "%PDF-test"}), 0600)
	if _, _, e := syncDocument(filepath.Join(mirror, "index.rmdoc"), "index", state, dest, nil, "", nil); e == nil {
		t.Fatal("PDF failure cached as success")
	}
	if _, e := os.Stat(filepath.Join(dest, "index.md")); !os.IsNotExist(e) {
		t.Fatal("partial note published")
	}
}
func TestHardeningRequiredOCRRetries(t *testing.T) {
	mirror, state, dest := fixture(t)
	input := filepath.Join(mirror, "index.rmdoc")
	os.WriteFile(input, bundle(t, map[string]string{"d.content": `{"fileType":"notebook"}`, "page.png": "fake image"}), 0600)

	for i := 0; i < 2; i++ {
		if _, _, e := syncDocument(input, "index", state, dest, ocr.New("://invalid", "", ""), "", nil); e == nil {
			t.Fatal("OCR failure cached as success")
		}
	}

}
func TestHardeningRunPropagatesErrors(t *testing.T) {
	installFakeRmapi(t)
	mirror, state, dest := fixture(t)
	os.WriteFile(filepath.Join(mirror, "broken.rmdoc"), []byte("not a zip"), 0600)
	if e := runSync(mirror, state, dest, nil, "", nil); e == nil {
		t.Fatal("partial conversion reported success")
	}
	if _, e := os.Stat(filepath.Join(dest, "index.md")); !os.IsNotExist(e) {
		t.Fatal("failed run published partial document")
	}
}
func TestHardeningArchiveRejectsUnsafeAndOversized(t *testing.T) {
	for _, entries := range []map[string]string{{"../escape.content": "{}"}, {"a.content": "not json"}, {"a.content": strings.Repeat(" ", 2<<20)}} {
		mirror, state, dest := fixture(t)
		input := filepath.Join(mirror, "index.rmdoc")
		os.WriteFile(input, bundle(t, entries), 0600)
		if _, _, e := syncDocument(input, "index", state, dest, nil, "", nil); e == nil {
			t.Fatal("unsafe or malformed archive accepted")
		}
	}
}
func TestHardeningIndexErrorPreservesLastGood(t *testing.T) {
	installFakeRmapi(t)
	mirror, state, dest := fixture(t)
	if e := runSync(mirror, state, dest, nil, "", nil); e != nil {
		t.Fatal(e)
	}
	note := filepath.Join(dest, "index.md")
	before, _ := os.ReadFile(note)
	os.MkdirAll(filepath.Join(dest, "_remarkable_collection"), 0700)
	os.Remove(filepath.Join(dest, "_remarkable_collection", "index.md"))
	os.Mkdir(filepath.Join(dest, "_remarkable_collection", "index.md"), 0700)
	seedRmdoc(t, filepath.Join(mirror, "index.rmdoc"), `{}`, `{"visibleName":"New"}`)
	if e := runSync(mirror, state, dest, nil, "", nil); e == nil {
		t.Fatal("index write error ignored")
	}
	after, _ := os.ReadFile(note)
	if !bytes.Equal(before, after) {
		t.Fatal("failed run changed last good output")
	}
}

func TestHardeningRejectsLinksAndPreexistingOutputs(t *testing.T) {
	for _, kind := range []string{"input-symlink", "input-hardlink", "output-symlink", "output-hardlink", "ancestor-symlink", "unowned-output"} {
		t.Run(kind, func(t *testing.T) {
			mirror, state, dest := fixture(t)
			input := filepath.Join(mirror, "index.rmdoc")
			outside := filepath.Join(t.TempDir(), "outside")
			os.WriteFile(outside, []byte("keep"), 0600)
			os.MkdirAll(dest, 0700)
			var err error
			switch kind {
			case "input-symlink":
				os.Remove(input)
				err = os.Symlink(outside, input)
			case "input-hardlink":
				os.Remove(input)
				err = os.Link(outside, input)
			case "output-symlink":
				err = os.Symlink(outside, filepath.Join(dest, "index.md"))
			case "output-hardlink":
				err = os.Link(outside, filepath.Join(dest, "index.md"))
			case "ancestor-symlink":
				os.Remove(dest)
				err = os.Symlink(filepath.Dir(outside), dest)
			case "unowned-output":
				err = os.WriteFile(filepath.Join(dest, "index.md"), []byte("my note"), 0600)
			}
			if err != nil {
				t.Fatal(err)
			}
			if _, _, err := syncDocument(input, "index", state, dest, nil, "", nil); err == nil {
				t.Fatal("unsafe path accepted")
			}
			got, _ := os.ReadFile(outside)
			if string(got) != "keep" {
				t.Fatal("outside file modified")
			}
		})
	}
}

func TestHardeningArchiveSymlinkAndFileCount(t *testing.T) {
	for _, kind := range []string{"symlink", "file-count"} {
		t.Run(kind, func(t *testing.T) {
			var b bytes.Buffer
			w := zip.NewWriter(&b)
			if kind == "symlink" {
				h := &zip.FileHeader{Name: "evil.content"}
				h.SetMode(os.ModeSymlink | 0777)
				f, _ := w.CreateHeader(h)
				f.Write([]byte("elsewhere"))
			} else {
				for i := 0; i < maxFiles+1; i++ {
					f, _ := w.Create(fmt.Sprintf("%d", i))
					f.Write(nil)
				}
			}
			w.Close()
			if _, _, err := readBundle(b.Bytes()); err == nil {
				t.Fatal("unsafe archive accepted")
			}
		})
	}
}

func TestHardeningDiskFaultRollsBackOutputAndState(t *testing.T) {
	for _, fault := range []string{"rename", "fsync"} {
		t.Run(fault, func(t *testing.T) {
			root := t.TempDir()
			note := filepath.Join(root, "note.md")
			state := filepath.Join(root, "state.json")
			os.WriteFile(note, []byte("last good"), 0640)
			os.WriteFile(state, []byte("old state"), 0600)
			changes := []mutation{{path: note, data: []byte("new bytes"), mode: 0640}, {path: state, data: []byte("new state"), mode: 0600}}
			rename := os.Rename
			flush := syncDir
			calls := 0
			if fault == "rename" {
				rename = func(a, b string) error {
					calls++
					if calls == 2 {
						return syscall.ENOSPC
					}
					return os.Rename(a, b)
				}
			} else {
				flush = func(p string) error {
					calls++
					if calls == 2 {
						return syscall.EIO
					}
					return syncDir(p)
				}
			}
			if err := applyChangesWithOps(changes, rename, flush); err == nil {
				t.Fatal("fault swallowed")
			}
			got, _ := os.ReadFile(note)
			saved, _ := os.ReadFile(state)
			if string(got) != "last good" || string(saved) != "old state" {
				t.Fatalf("last good output/state lost: %q, %q", got, saved)
			}
			files, _ := os.ReadDir(root)
			if len(files) != 2 {
				t.Fatal("staging files leaked")
			}
		})
	}
}

func TestHardeningOptionalOCRNeverMarksComplete(t *testing.T) {
	t.Setenv("REMARKABLE_OCR_POLICY", "optional")
	mirror, state, dest := fixture(t)
	input := filepath.Join(mirror, "index.rmdoc")
	os.WriteFile(input, bundle(t, map[string]string{"d.content": `{}`, "page.png": "image"}), 0600)
	for i := 0; i < 2; i++ {
		changed, _, err := syncDocument(input, "index", state, dest, ocr.New("://invalid", "", ""), "", nil)
		if err != nil {
			t.Fatal(err)
		}
		if !changed {
			t.Fatal("incomplete OCR reused as completed")
		}
	}
	saved, err := loadState(statePath(state, "index"))
	if err != nil {
		t.Fatal(err)
	}
	if saved.Complete {
		t.Fatal("incomplete output marked complete")
	}
	note, _ := os.ReadFile(filepath.Join(dest, "index.md"))
	if !bytes.Contains(note, []byte("OCR incomplete")) {
		t.Fatal("missing incomplete OCR notice")
	}
}

func TestHardeningRunSyncRemovesOnlyUnmodifiedOwnedPDF(t *testing.T) {
	for _, annotated := range []bool{false, true} {
		t.Run(fmt.Sprint(annotated), func(t *testing.T) {
			installFakeRmapi(t)
			mirror, state, dest := fixture(t)
			input := filepath.Join(mirror, "index.rmdoc")
			os.WriteFile(input, bundle(t, map[string]string{"d.content": `{"fileType":"pdf"}`, "d.pdf": "%PDF-last-good"}), 0600)
			if err := runSync(mirror, state, dest, nil, "", nil); err != nil {
				t.Fatal(err)
			}
			pdf := filepath.Join(dest, "index.pdf")
			note := filepath.Join(dest, "index.md")
			before, _ := os.ReadFile(note)
			if annotated {
				os.WriteFile(pdf, []byte("annotated PDF"), 0640)
			}
			os.WriteFile(input, bundle(t, map[string]string{"d.content": `{"fileType":"notebook"}`}), 0600)
			err := runSync(mirror, state, dest, nil, "", nil)
			if annotated {
				if err == nil {
					t.Fatal("annotated PDF removal accepted")
				}
				got, _ := os.ReadFile(pdf)
				after, _ := os.ReadFile(note)
				if string(got) != "annotated PDF" || !bytes.Equal(before, after) {
					t.Fatal("annotation or last good note changed")
				}
			} else {
				if err != nil {
					t.Fatal("owned PDF removal failed", err)
				}
				if _, err := os.Stat(pdf); !os.IsNotExist(err) {
					t.Fatal("obsolete owned PDF retained")
				}
				if err := runSync(mirror, state, dest, nil, "", nil); err != nil {
					t.Fatal("cache-only run after PDF removal failed", err)
				}
			}
		})
	}
}

func TestFullRunRetiresMissingLocalBundleWithoutDeletingAnnotations(t *testing.T) {
	for _, annotated := range []bool{false, true} {
		t.Run(fmt.Sprint(annotated), func(t *testing.T) {
			installFakeRmapi(t)
			mirror, state, dest := fixture(t)
			input := filepath.Join(mirror, "index.rmdoc")
			if err := runSync(mirror, state, dest, nil, "", nil); err != nil {
				t.Fatal(err)
			}
			note := filepath.Join(dest, "index.md")
			before, _ := os.ReadFile(note)
			published := filepath.Join(t.TempDir(), "published", "generations", "old", "files", "index.md")
			os.MkdirAll(filepath.Dir(published), 0750)
			os.WriteFile(published, before, 0640)
			if annotated {
				os.WriteFile(note, []byte("human annotation"), 0640)
			}
			originalBundle, _ := os.ReadFile(input)
			os.Remove(input)
			err := runSync(mirror, state, dest, nil, "", nil)
			if annotated {
				if err == nil {
					t.Fatal("annotated missing-source output retired")
				}
				got, _ := os.ReadFile(note)
				if string(got) != "human annotation" {
					t.Fatal("annotation deleted")
				}
			} else {
				if err != nil {
					t.Fatal("missing private bundle blocks acquisition", err)
				}
				if _, err := os.Stat(note); !os.IsNotExist(err) {
					t.Fatal("retired generated note remains active")
				}
				provenance, _ := os.ReadFile(filepath.Join(dest, collectionDir, "provenance.json"))
				if !bytes.Contains(provenance, []byte("local_bundle_absent")) {
					t.Fatal("missing local disappearance tombstone")
				}
				if err := runSync(mirror, state, dest, nil, "", nil); err != nil {
					t.Fatal("retired state blocks subsequent run", err)
				}
				seedRmdoc(t, input, `{}`, `{"visibleName":"Returned"}`)
				if err := runSync(mirror, state, dest, nil, "", nil); err != nil {
					t.Fatal("reappeared bundle cannot reactivate", err)
				}
			}
			archived, archiveErr := os.ReadFile(filepath.Join(filepath.Dir(mirror), "originals", digest(originalBundle)+".rmdoc"))
			if archiveErr != nil || !bytes.Equal(archived, originalBundle) {
				t.Fatal("local disappearance lost retained original", archiveErr)
			}
			old, _ := os.ReadFile(published)
			if !bytes.Equal(before, old) {
				t.Fatal("prior publication changed")
			}
		})
	}
}
func TestFullRunLabelsRetainedMirrorCloudPresenceUnverified(t *testing.T) {
	installFakeRmapi(t)
	mirror, state, dest := fixture(t)
	for i := 0; i < 2; i++ {
		if err := runSync(mirror, state, dest, nil, "", nil); err != nil {
			t.Fatal(err)
		}
	}
	note, _ := os.ReadFile(filepath.Join(dest, "index.md"))
	provenance, _ := os.ReadFile(filepath.Join(dest, collectionDir, "provenance.json"))
	if !bytes.Contains(note, []byte("remarkable_cloud_presence: unverified")) || !bytes.Contains(provenance, []byte("retained_local_mirror")) {
		t.Fatal("retained local bundle presented without provenance")
	}
	if _, err := os.Stat(filepath.Join(mirror, "index.rmdoc")); err != nil {
		t.Fatal("original bundle removed")
	}
}

func TestOriginalBundleArchiveSurvivesFailedDownloaderOverwrite(t *testing.T) {
	mirror, state, dest := fixture(t)
	input := filepath.Join(mirror, "index.rmdoc")
	original, _ := os.ReadFile(input)
	installFakeRmapi(t)
	if err := runSync(mirror, state, dest, nil, "", nil); err != nil {
		t.Fatal(err)
	}
	note := filepath.Join(dest, "index.md")
	before, _ := os.ReadFile(note)
	dir := t.TempDir()
	script := "#!/bin/sh\nprintf broken > '" + input + "'\nexit 1\n"
	os.WriteFile(filepath.Join(dir, "rmapi"), []byte(script), 0755)
	t.Setenv("PATH", dir+":"+os.Getenv("PATH"))
	if err := runSync(mirror, state, dest, nil, "", nil); err == nil {
		t.Fatal("failed downloader accepted")
	}
	archived, err := os.ReadFile(filepath.Join(filepath.Dir(mirror), "originals", digest(original)+".rmdoc"))
	if err != nil || !bytes.Equal(archived, original) {
		t.Fatal("original overwritten by failed download", err)
	}
	after, _ := os.ReadFile(note)
	if !bytes.Equal(before, after) {
		t.Fatal("failed download changed generated output")
	}
}

func TestOriginalArchiveProtectionAndInterruptedRetention(t *testing.T) {
	mirror, _, _ := fixture(t)
	archive := filepath.Join(filepath.Dir(mirror), "originals")
	if err := preserveOriginalBundles(mirror, archive); err != nil {
		t.Fatal(err)
	}
	entries, _ := os.ReadDir(archive)
	if len(entries) != 1 {
		t.Fatal("original not retained")
	}
	file := filepath.Join(archive, entries[0].Name())
	info, _ := os.Stat(file)
	if info.Mode().Perm() != 0400 {
		t.Fatal("original bundle remains writable")
	}
	err := acquireWithProtectedOriginals(archive, func() error {
		info, _ := os.Stat(archive)
		if info.Mode().Perm() != 0500 {
			t.Fatal("archive not protected during downloader")
		}
		return syscall.EIO
	})
	if !errors.Is(err, syscall.EIO) {
		t.Fatal(err)
	}
	info, _ = os.Stat(archive)
	if info.Mode().Perm() != 0700 {
		t.Fatal("converter archive access not restored")
	}
	os.WriteFile(filepath.Join(archive, ".atomic-interrupted"), []byte("incomplete retention"), 0600)
	if err := preserveOriginalBundles(mirror, archive); err != nil {
		t.Fatal("interrupted retention cannot recover", err)
	}
	if _, err := os.Stat(filepath.Join(archive, ".atomic-interrupted")); !os.IsNotExist(err) {
		t.Fatal("incomplete retention staging remains")
	}
}
