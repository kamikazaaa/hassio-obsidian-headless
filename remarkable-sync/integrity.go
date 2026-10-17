package main

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"time"

	"remarkable-sync/internal/ocr"
	"remarkable-sync/internal/rmapicli"
)

const (
	conversionVersion = "hardened-2"
	stateVersion      = "1"
	maxBundle         = 64 << 20
	maxExpanded       = 128 << 20
	maxMetadata       = 1 << 20
	maxImage          = 16 << 20
	maxPDF            = 64 << 20
	maxFiles          = 2048
	collectionDir     = "_remarkable_collection"
)

type imagePage struct {
	name string
	data []byte
}
type conversionState struct {
	Version       string            `json:"version"`
	SourceHash    string            `json:"source_hash,omitempty"`
	Retired       bool              `json:"retired,omitempty"`
	RetiredReason string            `json:"retired_reason,omitempty"`
	Key           string            `json:"key"`
	Complete      bool              `json:"complete"`
	Outputs       map[string]string `json:"outputs"`
	Summary       docSummary        `json:"summary"`
}
type mutation struct {
	path          string
	data          []byte
	mode          fs.FileMode
	remove        bool
	expected      string
	checkExpected bool
}
type plan struct {
	changed    bool
	summary    docSummary
	changes    []mutation
	outputs    map[string]string
	sourceHash string
}

func digest(b []byte) string { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }

func validRelative(name string, reserve bool) error {
	if name == "" || name == "." || filepath.IsAbs(name) || strings.ContainsAny(name, "\\\x00\r\n") || filepath.Clean(name) != name {
		return fmt.Errorf("unsafe relative path %q", name)
	}
	for _, p := range strings.Split(filepath.ToSlash(name), "/") {
		if p == "" || p == ".." || strings.HasPrefix(p, ".") {
			return fmt.Errorf("unsafe path component %q", p)
		}
	}
	if reserve && strings.Split(filepath.ToSlash(name), "/")[0] == collectionDir {
		return fmt.Errorf("reserved collection namespace: %s", name)
	}
	return nil
}

// Private generated/cache trees must not contain links or non-directory ancestors.
// Runtime gives this worker exclusive ownership; source/mcp users cannot mutate it.
func inspectPath(path string) error {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return err
	}
	current := string(filepath.Separator)
	parts := strings.Split(strings.TrimPrefix(absolute, string(filepath.Separator)), string(filepath.Separator))
	for i, p := range parts {
		current = filepath.Join(current, p)
		info, err := os.Lstat(current)
		if os.IsNotExist(err) {
			return nil
		}
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("symlink forbidden: %s", current)
		}
		if i < len(parts)-1 && !info.IsDir() {
			return fmt.Errorf("non-directory ancestor: %s", current)
		}
		if i == len(parts)-1 && !info.IsDir() {
			if !info.Mode().IsRegular() {
				return fmt.Errorf("special file forbidden: %s", current)
			}
			if st, ok := info.Sys().(*syscall.Stat_t); ok && st.Nlink != 1 {
				return fmt.Errorf("hardlink forbidden: %s", current)
			}
		}
	}
	return nil
}
func safeMkdir(path string, mode fs.FileMode) error {
	if err := inspectPath(path); err != nil {
		return err
	}
	var created []string
	for current := path; ; current = filepath.Dir(current) {
		if _, err := os.Lstat(current); os.IsNotExist(err) {
			created = append(created, current)
		} else if err != nil {
			return err
		} else {
			break
		}
		if filepath.Dir(current) == current {
			break
		}
	}
	if err := os.MkdirAll(path, mode); err != nil {
		return err
	}
	for i := len(created) - 1; i >= 0; i-- {
		if err := syncDir(filepath.Dir(created[i])); err != nil {
			return err
		}
	}
	return inspectPath(path)
}

// Open every Linux path component without following links. O_NONBLOCK avoids
// hanging if a regular file is concurrently replaced with a FIFO.
func openNoFollow(path string) (*os.File, error) {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	parts := strings.Split(strings.TrimPrefix(absolute, "/"), "/")
	fd, err := syscall.Open("/", syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_DIRECTORY, 0)
	if err != nil {
		return nil, err
	}
	for i, part := range parts {
		flags := syscall.O_RDONLY | syscall.O_CLOEXEC | syscall.O_NOFOLLOW | syscall.O_NONBLOCK
		if i < len(parts)-1 {
			flags |= syscall.O_DIRECTORY
		}
		next, err := syscall.Openat(fd, part, flags, 0)
		syscall.Close(fd)
		if err != nil {
			return nil, &os.PathError{Op: "open", Path: path, Err: err}
		}
		fd = next
	}
	return os.NewFile(uintptr(fd), absolute), nil
}

func readRegular(path string, limit int64) ([]byte, error) {
	if err := inspectPath(path); err != nil {
		return nil, err
	}
	f, err := openNoFollow(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Size() > limit {
		return nil, fmt.Errorf("invalid or oversized file: %s", path)
	}
	if st, ok := info.Sys().(*syscall.Stat_t); ok && st.Nlink != 1 {
		return nil, fmt.Errorf("hardlink forbidden: %s", path)
	}
	data, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > limit {
		return nil, fmt.Errorf("file exceeds limit: %s", path)
	}
	return data, nil
}

func readBundle(blob []byte) (blobMetadata, []imagePage, error) {
	var meta blobMetadata
	fail := func(err error) (blobMetadata, []imagePage, error) { return blobMetadata{}, nil, err }
	if len(blob) > maxBundle {
		return fail(errors.New("bundle exceeds compressed size limit"))
	}
	r, err := zip.NewReader(bytes.NewReader(blob), int64(len(blob)))
	if err != nil {
		return fail(fmt.Errorf("invalid bundle: %w", err))
	}
	if len(r.File) > maxFiles {
		return fail(errors.New("archive entry count limit exceeded"))
	}
	seen := map[string]bool{}
	kinds := map[string]bool{}
	var total uint64
	var pages []imagePage
	for _, f := range r.File {
		name := strings.TrimSuffix(f.Name, "/")
		if err := validRelative(name, false); err != nil {
			return fail(err)
		}
		if seen[name] {
			return fail(fmt.Errorf("duplicate archive member %q", name))
		}
		seen[name] = true
		if f.Mode()&os.ModeSymlink != 0 || (!f.FileInfo().IsDir() && !f.Mode().IsRegular()) {
			return fail(fmt.Errorf("non-regular archive member %q", name))
		}
		if f.FileInfo().IsDir() {
			continue
		}
		ext := strings.ToLower(filepath.Ext(name))
		limit := uint64(32 << 20)
		switch ext {
		case ".content", ".metadata":
			limit = maxMetadata
		case ".pdf":
			limit = maxPDF
		case ".png", ".jpg", ".jpeg":
			limit = maxImage
		}
		if f.UncompressedSize64 > limit || f.UncompressedSize64 > maxExpanded-total {
			return fail(fmt.Errorf("archive expansion limit: %s", name))
		}
		total += f.UncompressedSize64
		rc, err := f.Open()
		if err != nil {
			return fail(err)
		}
		data, readErr := io.ReadAll(io.LimitReader(rc, int64(limit)+1))
		closeErr := rc.Close()
		if readErr != nil {
			return fail(readErr)
		}
		if closeErr != nil {
			return fail(closeErr)
		}
		if uint64(len(data)) > limit {
			return fail(errors.New("archive member exceeds limit"))
		}
		switch ext {
		case ".content":
			if kinds[ext] {
				return fail(errors.New("multiple content records"))
			}
			kinds[ext] = true
			var c rmContent
			if err := json.Unmarshal(data, &c); err != nil {
				return fail(fmt.Errorf("content JSON: %w", err))
			}
			if c.PageCount < 0 || c.PageCount > 100000 || len(c.Tags) > 10000 {
				return fail(errors.New("metadata count limit"))
			}
			meta.PageCount = c.PageCount
			meta.FileType = c.FileType
			for _, t := range c.Tags {
				meta.Tags = append(meta.Tags, t.Name)
			}
		case ".metadata":
			if kinds[ext] {
				return fail(errors.New("multiple metadata records"))
			}
			kinds[ext] = true
			var m rmMetadata
			if err := json.Unmarshal(data, &m); err != nil {
				return fail(fmt.Errorf("metadata JSON: %w", err))
			}
			meta.VisibleName = m.VisibleName
			meta.Pinned = m.Pinned
			if m.LastModified != "" {
				parsed, err := parseEpochMillis(m.LastModified)
				if err != nil {
					return fail(err)
				}
				meta.Modified = parsed
			}
		case ".pdf":
			if kinds[ext] {
				return fail(errors.New("multiple embedded PDFs"))
			}
			kinds[ext] = true
			meta.PDF = data
		case ".png", ".jpg", ".jpeg":
			if len(pages) >= 256 {
				return fail(errors.New("embedded page image count limit"))
			}
			pages = append(pages, imagePage{name, data})
		}
	}
	if !kinds[".content"] {
		return fail(errors.New("missing content record"))
	}
	if meta.FileType == "pdf" && len(meta.PDF) == 0 {
		return fail(errors.New("PDF document missing required embedded PDF"))
	}
	sort.Slice(pages, func(i, j int) bool { return pages[i].name < pages[j].name })
	return meta, pages, nil
}

func loadState(path string) (*conversionState, error) {
	data, err := readRegular(path, maxMetadata)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var s conversionState
	if err := json.Unmarshal(data, &s); err != nil {
		return nil, fmt.Errorf("invalid conversion state: %w", err)
	}
	if s.Version != stateVersion || s.Outputs == nil {
		return nil, errors.New("unknown conversion state version; preserve outputs and reset through explicit migration")
	}
	for rel, hash := range s.Outputs {
		if err := validRelative(rel, false); err != nil {
			return nil, err
		}
		if !validHash(hash) {
			return nil, errors.New("invalid output hash in conversion state")
		}
	}
	return &s, nil
}
func statePath(stateRoot, name string) string {
	return filepath.Join(stateRoot, digest([]byte(name))+".json")
}
func checkManaged(dest string, old *conversionState) (bool, error) {
	missing := false
	if old == nil {
		return true, nil
	}
	for rel, want := range old.Outputs {
		if err := validRelative(rel, false); err != nil {
			return false, err
		}
		data, err := readRegular(filepath.Join(dest, rel), maxExpanded)
		if os.IsNotExist(err) {
			missing = true
			continue
		}
		if err != nil {
			return false, err
		}
		if digest(data) != want {
			return false, fmt.Errorf("manual output modification detected: %s; preserve or move annotations before retrying", rel)
		}
	}
	return missing, nil
}
func ownedChanges(dest string, old *conversionState, outputs map[string][]byte) ([]mutation, error) {
	var changes []mutation
	for rel, data := range outputs {
		target := filepath.Join(dest, rel)
		if err := inspectPath(target); err != nil {
			return nil, err
		}
		info, err := os.Lstat(target)
		if err == nil {
			if !info.Mode().IsRegular() {
				return nil, fmt.Errorf("output is not a regular file: %s", rel)
			}
			if old == nil || old.Outputs[rel] == "" {
				return nil, fmt.Errorf("refusing to adopt existing output: %s", rel)
			}
		} else if !os.IsNotExist(err) {
			return nil, err
		}
		expected := ""
		if old != nil && err == nil {
			expected = old.Outputs[rel]
		}
		changes = append(changes, mutation{path: target, data: data, mode: 0640, expected: expected, checkExpected: true})
	}
	if old != nil {
		for rel := range old.Outputs {
			if _, ok := outputs[rel]; !ok {
				target := filepath.Join(dest, rel)
				actual, err := currentHash(target)
				if err != nil {
					return nil, err
				}
				if actual != "" && actual != old.Outputs[rel] {
					return nil, fmt.Errorf("manual output modification detected: %s", rel)
				}
				changes = append(changes, mutation{path: target, remove: true, mode: 0640, expected: actual, checkExpected: true})
			}
		}
	}
	return changes, nil
}
func planDocument(mirrorPath, relPath, stampDir, destRoot string, client *ocr.Client, prefix string, tags []string) (plan, error) {
	var p plan
	if err := validRelative(relPath, true); err != nil {
		return p, err
	}
	blob, err := readRegular(mirrorPath, maxBundle)
	if err != nil {
		return p, err
	}
	info, err := os.Stat(mirrorPath)
	if err != nil {
		return p, err
	}
	stateFile := statePath(stampDir, relPath)
	old, err := loadState(stateFile)
	if err != nil {
		return p, err
	}
	if err := validateDocumentState(old, relPath); err != nil {
		return p, err
	}
	p.sourceHash = digest(blob)

	missing, err := checkManaged(destRoot, old)
	if err != nil {
		return p, err
	}
	fingerprint := "disabled"
	if client != nil {
		fingerprint = client.Fingerprint()
	}
	config, _ := json.Marshal([]any{conversionVersion, prefix, tags, fingerprint, env("REMARKABLE_OCR_POLICY", "required")})
	key := digest(append([]byte(digest(blob)), config...))
	if old != nil && !old.Retired && old.Complete && old.Key == key && !missing {
		p.summary = old.Summary
		p.outputs = old.Outputs
		return p, nil
	}
	meta, pages, err := readBundle(blob)
	if err != nil {
		return p, err
	}
	p.summary = docSummary{RelPath: relPath, Title: filepath.Base(relPath), Modified: info.ModTime()}
	if meta.VisibleName != "" {
		p.summary.Title = meta.VisibleName
	}
	if !meta.Modified.IsZero() {
		p.summary.Modified = meta.Modified
	}
	meta.SourceHash = p.sourceHash
	meta.Tags = mapTags(meta.Tags, prefix, tags)
	var texts []string
	complete := true
	ocrBytes := 0
	for _, page := range pages {
		if client == nil {
			break
		}
		text, err := client.Recognise(page.data)
		if err != nil {
			if env("REMARKABLE_OCR_POLICY", "required") != "optional" {
				return p, fmt.Errorf("required OCR failed for %s: %w", page.name, err)
			}
			complete = false
			continue
		}
		if text != "" {
			ocrBytes += len(text)
			if ocrBytes > 8<<20 {
				return p, errors.New("document OCR text size limit")
			}
			texts = append(texts, text)
		}
	}
	text := strings.Join(texts, "\n\n---\n\n")
	if !complete {
		text = "*OCR incomplete: some embedded page images failed; conversion will be retried.*\n\n" + text
	}
	outputs := map[string][]byte{relPath + ".md": renderNote(p.summary.Title, relPath, p.summary.Modified, meta, text)}
	if len(meta.PDF) > 0 {
		outputs[relPath+".pdf"] = meta.PDF
	}
	changes, err := ownedChanges(destRoot, old, outputs)
	if err != nil {
		return p, err
	}
	next := conversionState{Version: stateVersion, SourceHash: p.sourceHash, Key: key, Complete: complete, Summary: p.summary, Outputs: map[string]string{}}
	for name, data := range outputs {
		next.Outputs[name] = digest(data)
	}
	data, err := json.Marshal(next)
	if err != nil {
		return p, err
	}
	if len(data) > maxMetadata {
		return p, errors.New("document conversion state size limit")
	}
	p.changes = append(changes, mutation{path: stateFile, data: data, mode: 0600})
	p.outputs = next.Outputs
	p.changed = true
	return p, nil
}

func syncDocument(mirrorPath, relPath, stampDir, destRoot string, client *ocr.Client, prefix string, tags []string) (bool, docSummary, error) {
	if err := distinctTrees(mirrorPath, stampDir, destRoot); err != nil {
		return false, docSummary{}, err
	}
	if err := safeMkdir(stampDir, 0700); err != nil {
		return false, docSummary{}, err
	}
	lock, err := acquireConversionLock(stampDir)
	if err != nil {
		return false, docSummary{}, err
	}
	defer lock.Close()
	if err := recoverTransaction(stampDir, destRoot); err != nil {
		return false, docSummary{}, err
	}
	p, err := planDocument(mirrorPath, relPath, stampDir, destRoot, client, prefix, tags)
	if err != nil {
		return false, p.summary, err
	}
	if err := applyTransaction(stampDir, destRoot, p.changes); err != nil {
		return false, p.summary, err
	}
	return p.changed, p.summary, nil
}

// Mirror validation runs before invoking rmapi as well as after download, so
// pre-existing links cannot redirect the child process into another workspace.
func validateMirror(root string) error {
	count := 0
	var total int64
	return filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if err := inspectPath(path); err != nil {
			return err
		}
		if path != root {
			rel, err := filepath.Rel(root, path)
			if err != nil {
				return err
			}
			if err := validRelative(rel, false); err != nil {
				return err
			}
		}
		count++
		if count > 20000 {
			return errors.New("mirror entry count limit")
		}
		if !d.IsDir() {
			info, err := d.Info()
			if err != nil {
				return err
			}
			if !info.Mode().IsRegular() {
				return fmt.Errorf("nonregular mirror file: %s", path)
			}
			total += info.Size()
			if info.Size() > maxBundle || total > 512<<20 {
				return errors.New("mirror size limit")
			}
		}
		return nil
	})
}
func distinctTrees(paths ...string) error {
	for i, a := range paths {
		aa, err := filepath.Abs(a)
		if err != nil {
			return err
		}
		for _, b := range paths[i+1:] {
			bb, err := filepath.Abs(b)
			if err != nil {
				return err
			}
			for _, pair := range [][2]string{{aa, bb}, {bb, aa}} {
				rel, err := filepath.Rel(pair[0], pair[1])
				if err != nil {
					return err
				}
				if rel == "." || (rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))) {
					return errors.New("mirror, state and generated trees must be distinct and nonoverlapping")
				}
			}
		}
	}
	return nil
}
func validateGenerated(root string, owned map[string]bool) error {
	return filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if err := inspectPath(path); err != nil {
			return err
		}
		if path == root {
			return nil
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		if err := validRelative(rel, false); err != nil {
			return err
		}
		if !d.IsDir() && !owned[rel] {
			return fmt.Errorf("unmanaged generated file: %s; preserve it outside the generated tree before retrying", rel)
		}
		return nil
	})
}

func runSync(mirrorDir, stampDir, destRoot string, client *ocr.Client, prefix string, tags []string) error {
	if err := distinctTrees(mirrorDir, stampDir, destRoot); err != nil {
		return err
	}
	for _, dir := range []string{mirrorDir, stampDir, destRoot} {
		if err := safeMkdir(dir, 0700); err != nil {
			return err
		}
	}
	lock, err := acquireConversionLock(stampDir)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err := recoverTransaction(stampDir, destRoot); err != nil {
		return err
	}
	if err := validateMirror(mirrorDir); err != nil {
		return err
	}
	if err := preserveOriginalBundles(mirrorDir, filepath.Join(filepath.Dir(mirrorDir), "originals")); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()
	if err := acquireWithProtectedOriginals(filepath.Join(filepath.Dir(mirrorDir), "originals"), func() error { return rmapicli.SyncTree(ctx, "/", mirrorDir) }); err != nil {
		return fmt.Errorf("rmapi sync: %w", err)
	}
	if err := validateMirror(mirrorDir); err != nil {
		return err
	}
	if err := preserveOriginalBundles(mirrorDir, filepath.Join(filepath.Dir(mirrorDir), "originals")); err != nil {
		return err
	}
	owned := map[string]bool{}
	present := map[string]string{}
	var docs []docSummary
	var changes []mutation
	var size int64
	var outputSize int64
	count := 0
	err = filepath.WalkDir(mirrorDir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if err := inspectPath(path); err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		if !d.Type().IsRegular() {
			return fmt.Errorf("nonregular mirror member: %s", path)
		}
		rel, err := filepath.Rel(mirrorDir, path)
		if err != nil {
			return err
		}
		if err := validRelative(rel, false); err != nil {
			return err
		}
		if strings.ToLower(filepath.Ext(path)) != ".rmdoc" {
			return nil
		}
		count++
		info, err := d.Info()
		if err != nil {
			return err
		}
		size += info.Size()
		if count > 10000 || size > 512<<20 {
			return errors.New("mirror acquisition size/count limit")
		}
		rel = strings.TrimSuffix(rel, filepath.Ext(rel))
		p, err := planDocument(path, rel, stampDir, destRoot, client, prefix, tags)
		if err != nil {
			return fmt.Errorf("convert %q: %w", rel, err)
		}
		present[rel] = p.sourceHash
		for rel := range p.outputs {
			owned[rel] = true
		}
		docs = append(docs, p.summary)
		for _, m := range p.changes {
			// An output scheduled for removal is still owned until commit.
			// The planner has already checked its prior ownership and hash.
			if m.remove && m.mode == 0640 {
				rel, err := filepath.Rel(destRoot, m.path)
				if err != nil {
					return err
				}
				if err := validRelative(rel, false); err != nil {
					return err
				}
				owned[rel] = true
			}
			outputSize += int64(len(m.data))
		}
		if outputSize > 512<<20 {
			return errors.New("generated output size limit")
		}
		changes = append(changes, p.changes...)
		return nil
	})
	if err != nil {
		return err
	}
	retirements, tombstones, err := planLocalRetirements(stampDir, destRoot, present)
	if err != nil {
		return err
	}
	changes = append(changes, retirements...)
	for _, m := range retirements {
		if m.remove && m.mode == 0640 {
			rel, err := filepath.Rel(destRoot, m.path)
			if err != nil {
				return err
			}
			if err := validRelative(rel, false); err != nil {
				return err
			}
			owned[rel] = true
		}
	}
	sort.Slice(docs, func(i, j int) bool { return docs[i].RelPath < docs[j].RelPath })
	indexFile := filepath.Join(stampDir, "collection.json")
	old, err := loadState(indexFile)
	if err != nil {
		return err
	}
	if old != nil && (len(old.Outputs) > 2 || old.Outputs[filepath.Join(collectionDir, "index.md")] == "") {
		return errors.New("invalid collection index ownership state")
	}
	if old != nil {
		for rel := range old.Outputs {
			if rel != filepath.Join(collectionDir, "index.md") && rel != filepath.Join(collectionDir, "provenance.json") {
				return errors.New("unexpected collection output ownership")
			}
		}
	}
	if _, err := checkManaged(destRoot, old); err != nil {
		return err
	}
	provenance, err := json.MarshalIndent(sourceProvenance{SchemaVersion: 1, AcquisitionSemantics: acquisitionSemantics, CloudPresence: "unverified", ActiveBundles: present, Retired: tombstones}, "", "  ")
	if err != nil {
		return err
	}
	outputs := map[string][]byte{filepath.Join(collectionDir, "index.md"): renderIndex(docs), filepath.Join(collectionDir, "provenance.json"): provenance}
	indexChanges, err := ownedChanges(destRoot, old, outputs)
	if err != nil {
		return err
	}
	next := conversionState{Version: stateVersion, Complete: true, Outputs: map[string]string{}}
	for rel, data := range outputs {
		next.Outputs[rel] = digest(data)
		owned[rel] = true
	}
	if err := validateGenerated(destRoot, owned); err != nil {
		return err
	}
	data, err := json.Marshal(next)
	if err != nil {
		return err
	}
	changes = append(changes, indexChanges...)
	changes = append(changes, mutation{path: indexFile, data: data, mode: 0600})
	return applyTransaction(stampDir, destRoot, changes)
}

// Prepare and fsync every replacement before changing any existing output. On
// ordinary commit failures restore the previous bytes. State is committed last.
// The runtime publishes only after this entire one-shot process exits success.
func applyChanges(changes []mutation) error {
	return applyChangesWithOps(changes, os.Rename, syncDir)
}

func applyChangesWithOps(changes []mutation, rename func(string, string) error, flushDir func(string) error) error {
	return applyChangesStaged(changes, rename, flushDir, "")
}

func applyChangesStaged(changes []mutation, rename func(string, string) error, flushDir func(string) error, stageRoot string) error {
	if len(changes) == 0 {
		return nil
	}
	sort.SliceStable(changes, func(i, j int) bool { return changes[i].mode > changes[j].mode })
	type prepared struct {
		m       mutation
		temp    string
		prior   []byte
		existed bool
		done    bool
	}
	pending := make([]prepared, 0, len(changes))
	seen := map[string]bool{}
	defer func() {
		for _, p := range pending {
			if p.temp != "" {
				os.Remove(p.temp)
			}
		}
	}()
	for _, m := range changes {
		if seen[m.path] {
			return fmt.Errorf("colliding output path: %s", m.path)
		}
		seen[m.path] = true
		if err := inspectPath(m.path); err != nil {
			return err
		}
		var prior []byte
		existed := false
		if _, err := os.Lstat(m.path); err == nil {
			var readErr error
			prior, readErr = readRegular(m.path, maxExpanded)
			if readErr != nil {
				return readErr
			}
			existed = true
		} else if !os.IsNotExist(err) {
			return err
		}
		actualHash := ""
		if existed {
			actualHash = digest(prior)
		}
		if m.checkExpected && actualHash != m.expected {
			return fmt.Errorf("output changed before transaction: %s", m.path)
		}
		if err := safeMkdir(filepath.Dir(m.path), 0700); err != nil {
			return err
		}
		p := prepared{m: m, prior: prior, existed: existed}
		if !m.remove {
			tempDir := filepath.Dir(m.path)
			if stageRoot != "" {
				tempDir = stageRoot
			}
			f, err := os.CreateTemp(tempDir, ".conversion-*")
			if err != nil {
				return err
			}
			p.temp = f.Name()
			pending = append(pending, p)
			if err := f.Chmod(m.mode); err != nil {
				f.Close()
				return err
			}
			if _, err := f.Write(m.data); err != nil {
				f.Close()
				return err
			}
			if err := f.Sync(); err != nil {
				f.Close()
				return err
			}
			if err := f.Close(); err != nil {
				return err
			}
		} else {
			pending = append(pending, p)
		}
	}
	rollback := func(cause error) error {
		for i := len(pending) - 1; i >= 0; i-- {
			p := pending[i]
			if !p.done {
				continue
			}
			expectedNew := ""
			if !p.m.remove {
				expectedNew = digest(p.m.data)
			}
			actual, hashErr := currentHash(p.m.path)
			if hashErr != nil || actual != expectedNew {
				cause = errors.Join(cause, fmt.Errorf("manual change conflicts with rollback: %s", p.m.path))
				continue
			}
			var err error
			if p.existed {
				err = atomicWriteStaged(p.m.path, p.prior, p.m.mode, stageRoot)
			} else {
				err = os.Remove(p.m.path)
				if os.IsNotExist(err) {
					err = nil
				}
			}
			// A deleted newly-created target needs the same durable parent
			// update as an atomic restoration. Keep the failure visible so
			// journal recovery can retry it before discarding backups.
			if err == nil {
				err = flushDir(filepath.Dir(p.m.path))
			}
			if err != nil {
				cause = errors.Join(cause, fmt.Errorf("rollback %s: %w", p.m.path, err))
			}
		}
		return cause
	}
	for i := range pending {
		p := &pending[i]
		// Recheck the expected original immediately before replacing it.
		current, err := readRegular(p.m.path, maxExpanded)
		if p.existed {
			if err != nil || !bytes.Equal(current, p.prior) {
				return rollback(fmt.Errorf("output changed during conversion: %s", p.m.path))
			}
		} else if !os.IsNotExist(err) {
			return rollback(fmt.Errorf("output appeared during conversion: %s", p.m.path))
		}
		if p.m.remove {
			if p.existed {
				err = os.Remove(p.m.path)
			} else {
				err = nil
			}
		} else {
			err = rename(p.temp, p.m.path)
		}
		if err != nil {
			return rollback(err)
		}
		p.done = true
		if err := flushDir(filepath.Dir(p.m.path)); err != nil {
			return rollback(err)
		}
	}
	return nil
}
func syncDir(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	return f.Sync()
}
func atomicWrite(path string, data []byte, mode fs.FileMode) error {
	return atomicWriteStaged(path, data, mode, "")
}

func atomicWriteStaged(path string, data []byte, mode fs.FileMode, stageRoot string) error {
	if err := inspectPath(path); err != nil {
		return err
	}
	if err := safeMkdir(filepath.Dir(path), 0700); err != nil {
		return err
	}
	tempDir := filepath.Dir(path)
	if stageRoot != "" {
		tempDir = stageRoot
	}
	f, err := os.CreateTemp(tempDir, ".atomic-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if err := f.Chmod(mode); err != nil {
		f.Close()
		return err
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Rename(f.Name(), path); err != nil {
		return err
	}
	return syncDir(filepath.Dir(path))
}
