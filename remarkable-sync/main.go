// remarkable-sync converts private rmapi bundles into a dedicated generated tree.
// SYNC_ONCE=true performs one acquisition; publication is handled by the runtime.
package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"remarkable-sync/internal/ocr"
	"remarkable-sync/internal/rmapicli"
)

func main() {
	vault := flag.String("vault-path", env("VAULT_PATH", "/data/remarkable/generated"), "dedicated generated tree")
	output := flag.String("output-dir", env("REMARKABLE_OUTPUT_DIR", "documents"), "relative generated subdirectory")
	cache := flag.String("cache-dir", env("REMARKABLE_CACHE_DIR", "/data/remarkable/private"), "private mirror and conversion state")
	endpoint := flag.String("ha-ocr-url", "", "optional OCR endpoint for embedded page images")
	token := env("HA_OCR_TOKEN", "")
	entity := flag.String("ha-ocr-entity", "", "OCR entity")
	prefix := flag.String("tag-prefix", env("REMARKABLE_TAG_PREFIX", ""), "tag prefix")
	tags := flag.String("extra-tags", env("REMARKABLE_EXTRA_TAGS", ""), "comma separated tags")
	continuous := flag.Bool("continuous", false, "repeat acquisition")
	interval := flag.Int("interval", envInt("SYNC_INTERVAL", 300), "interval seconds")
	flag.Parse()
	if *endpoint == "" {
		*endpoint = env("HA_OCR_URL", "")
	}
	if *entity == "" {
		*entity = env("HA_OCR_ENTITY", "")
	}
	if env("REMARKABLE_REGISTER_PORT", "") != "" || env("OBSIDIAN_AUTH_PORT", "") != "" {
		log.Fatal("obsolete credential-server options are unsupported; use managed addon options")
	}
	if env("REMARKABLE_SYNC_ENABLED", "true") == "false" {
		return
	}
	if err := validRelative(*output, false); err != nil {
		log.Fatal(err)
	}
	if *vault == "" || *cache == "" || *interval < 1 {
		log.Fatal("invalid paths or sync interval")
	}
	if err := safeMkdir(*cache, 0700); err != nil {
		log.Fatal(err)
	}
	if !rmapicli.HasCredentials() {
		log.Fatal("reMarkable is not paired; configure pairing through managed addon options")
	}
	var client *ocr.Client
	if *endpoint != "" {
		client = ocr.New(*endpoint, token, *entity)
	}
	if policy := env("REMARKABLE_OCR_POLICY", "required"); policy != "required" && policy != "optional" {
		log.Fatal("invalid REMARKABLE_OCR_POLICY")
	}
	once := env("SYNC_ONCE", "false") == "true" || !*continuous
	for {
		if err := runSync(filepath.Join(*cache, "mirror"), filepath.Join(*cache, "state"), filepath.Join(*vault, *output), client, *prefix, splitTags(*tags)); err != nil {
			if once {
				log.Fatal(err)
			}
			log.Printf("sync failed: %v", err)
		}
		if once {
			return
		}
		time.Sleep(time.Duration(*interval) * time.Second)
	}
}

// splitTags parses a comma-separated tag list, trimming whitespace and
// dropping empty entries.
func splitTags(raw string) []string {
	if raw == "" {
		return nil
	}
	var tags []string
	for _, t := range strings.Split(raw, ",") {
		if t = strings.TrimSpace(t); t != "" {
			tags = append(tags, t)
		}
	}
	return tags
}

// mapTags applies the configured label prefix to each reMarkable tag (if
// set — otherwise tags are overlaid into Obsidian unchanged) and appends any
// extra tags configured to apply to every synced document, deduplicating.
func mapTags(tags []string, prefix string, extraTags []string) []string {
	seen := make(map[string]bool, len(tags)+len(extraTags))
	var out []string
	add := func(t string) {
		if t == "" || seen[t] {
			return
		}
		seen[t] = true
		out = append(out, t)
	}
	for _, t := range tags {
		add(prefix + t)
	}
	for _, t := range extraTags {
		add(t)
	}
	return out
}

// docSummary describes one synced document for the vault-root index.
type docSummary struct {
	RelPath  string // relative to destRoot, no extension
	Title    string
	Modified time.Time
}

// blobMetadata holds structured data extracted from a reMarkable .rmdoc bundle.
type blobMetadata struct {
	SourceHash string
	// PageCount from the .content JSON.
	PageCount int
	// Tags from the .content JSON.
	Tags []string
	// FileType: "notebook", "pdf", "epub".
	FileType string
	// PDF contains the raw bytes of an embedded PDF document (may be nil).
	PDF []byte
	// VisibleName, Modified, Pinned come from the .metadata JSON, if present.
	VisibleName string
	Modified    time.Time
	Pinned      bool
}

// rmContent mirrors the fields we care about in reMarkable's .content JSON.
type rmContent struct {
	FileType  string  `json:"fileType"`
	PageCount int     `json:"pageCount"`
	Tags      []rmTag `json:"tags"`
}

type rmTag struct {
	Name string `json:"name"`
}

// rmMetadata mirrors the fields we care about in reMarkable's .metadata JSON.
type rmMetadata struct {
	VisibleName  string `json:"visibleName"`
	LastModified string `json:"lastModified"` // epoch milliseconds, as a string
	Pinned       bool   `json:"pinned"`
}

// extractBlobMetadata is retained for callers that only need metadata. Production
// conversion uses readBundle and propagates every validation error.
func extractBlobMetadata(blob []byte) blobMetadata {
	meta, _, err := readBundle(blob)
	if err != nil {
		return blobMetadata{}
	}
	return meta
}

func parseEpochMillis(s string) (time.Time, error) {
	ms, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return time.Time{}, err
	}
	return time.UnixMilli(ms), nil
}

// writeNote renders the markdown file for a document.
func writeNote(path, title, relPath string, modified time.Time, meta blobMetadata, ocrText string) error {
	return atomicWrite(path, renderNote(title, relPath, modified, meta, ocrText), 0640)
}

func renderNote(title, relPath string, modified time.Time, meta blobMetadata, ocrText string) []byte {
	var sb strings.Builder

	sb.WriteString("---\n")
	sb.WriteString(fmt.Sprintf("title: %q\n", title))
	sb.WriteString(fmt.Sprintf("modified: %s\n", modified.UTC().Format(time.RFC3339)))
	sb.WriteString(fmt.Sprintf("remarkable_path: %q\n", relPath))
	if meta.FileType != "" {
		sb.WriteString(fmt.Sprintf("remarkable_type: %q\n", meta.FileType))
	}
	if meta.PageCount > 0 {
		sb.WriteString(fmt.Sprintf("page_count: %d\n", meta.PageCount))
	}
	if len(meta.Tags) > 0 {
		sb.WriteString("tags:\n")
		for _, t := range meta.Tags {
			sb.WriteString(fmt.Sprintf("  - %q\n", t))
		}
	}
	if meta.Pinned {
		sb.WriteString("bookmarked: true\n")
	}
	sb.WriteString("source: reMarkable\nremarkable_acquisition_semantics: retained_local_mirror\nremarkable_cloud_presence: unverified\n")
	if meta.SourceHash != "" {
		sb.WriteString(fmt.Sprintf("remarkable_bundle_sha256: %q\n", meta.SourceHash))
	}
	sb.WriteString("---\n\n")

	sb.WriteString("# " + title + "\n\n")

	sb.WriteString("| Field | Value |\n")
	sb.WriteString("|---|---|\n")
	sb.WriteString(fmt.Sprintf("| Modified | %s |\n", modified.UTC().Format("2006-01-02 15:04 UTC")))
	if meta.PageCount > 0 {
		sb.WriteString(fmt.Sprintf("| Pages | %d |\n", meta.PageCount))
	}
	if meta.FileType != "" {
		sb.WriteString(fmt.Sprintf("| Type | %s |\n", meta.FileType))
	}
	if len(meta.PDF) > 0 {
		sb.WriteString(fmt.Sprintf("| PDF | [[%s.pdf]] |\n", filepath.Base(relPath)))
	}
	sb.WriteString("\n")

	if ocrText != "" {
		sb.WriteString("## Content (OCR)\n\n")
		sb.WriteString(ocrText)
		sb.WriteString("\n")
	} else if meta.FileType == "notebook" || meta.FileType == "" {
		sb.WriteString("*Native reMarkable pen-stroke transcription is unsupported. OCR applies only to embedded PNG/JPEG page images.*\n")
	}

	return []byte(sb.String())
}

// The collection index lives in a reserved namespace separate from documents.
func renderIndex(docs []docSummary) []byte {
	var sb strings.Builder
	sb.WriteString("---\ntitle: reMarkable Index\nsource: reMarkable\n---\n\n# reMarkable Index\n\nRetained local mirror; current cloud presence is unverified. Remotely deleted documents can remain here. Local disappearance tombstones and bundle hashes are recorded in `provenance.json`.\n\n| Document | Modified |\n|---|---|\n")
	for _, d := range docs {
		sb.WriteString(fmt.Sprintf("| [[%s]] | %s |\n", strings.ReplaceAll(filepath.ToSlash(d.RelPath), "|", "\\|"), d.Modified.UTC().Format("2006-01-02")))
	}
	return []byte(sb.String())
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		var n int
		if _, err := fmt.Sscanf(v, "%d", &n); err == nil {
			return n
		}
	}
	return def
}
