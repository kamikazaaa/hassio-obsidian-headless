package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
)

const acquisitionSemantics = "retained_local_mirror"
const localAbsentReason = "local_bundle_absent"

type retiredSource struct {
	Path       string `json:"path"`
	SourceHash string `json:"bundle_sha256,omitempty"`
	Reason     string `json:"reason"`
}
type sourceProvenance struct {
	SchemaVersion        int               `json:"schema_version"`
	AcquisitionSemantics string            `json:"acquisition_semantics"`
	CloudPresence        string            `json:"cloud_presence"`
	ActiveBundles        map[string]string `json:"active_local_bundles_sha256"`
	Retired              []retiredSource   `json:"local_tombstones"`
}

func validateDocumentState(old *conversionState, rel string) error {
	if old == nil {
		return nil
	}
	if old.Summary.RelPath != rel || !validHash(old.Key) || (old.SourceHash != "" && !validHash(old.SourceHash)) {
		return errors.New("invalid document ownership state")
	}
	if old.Retired {
		if len(old.Outputs) != 0 || old.RetiredReason != localAbsentReason {
			return errors.New("invalid retired ownership state")
		}
		return nil
	}
	if old.Outputs[rel+".md"] == "" || len(old.Outputs) > 2 {
		return errors.New("invalid document ownership state")
	}
	for output := range old.Outputs {
		if output != rel+".md" && output != rel+".pdf" {
			return errors.New("unexpected output ownership state")
		}
	}
	return nil
}

// Local disappearance is not proof of cloud deletion. Retire only previously
// authenticated generated outputs, retaining a private tombstone and published
// provenance. No mirrored bundle or prior immutable publication is removed.
func planLocalRetirements(stateRoot, destRoot string, present map[string]string) ([]mutation, []retiredSource, error) {
	entries, err := os.ReadDir(stateRoot)
	if err != nil {
		return nil, nil, err
	}
	if len(entries) > 10002 {
		return nil, nil, errors.New("conversion state/tombstone count limit")
	}
	union := map[string]bool{}
	for rel := range present {
		union[rel] = true
	}
	var changes []mutation
	retired := []retiredSource{}
	for _, entry := range entries {
		name := entry.Name()
		if name == ".conversion.lock" || name == "collection.json" {
			continue
		}
		if entry.IsDir() || !stateNamePattern.MatchString(name) {
			return nil, nil, fmt.Errorf("unexpected private conversion state: %s", name)
		}
		path := filepath.Join(stateRoot, name)
		old, err := loadState(path)
		if err != nil {
			return nil, nil, err
		}
		if old == nil {
			return nil, nil, errors.New("conversion state disappeared")
		}
		rel := old.Summary.RelPath
		union[rel] = true
		if len(union) > 10000 {
			return nil, nil, errors.New("active and retired source count limit")
		}
		if err := validRelative(rel, true); err != nil {
			return nil, nil, err
		}
		if statePath(stateRoot, rel) != path {
			return nil, nil, errors.New("conversion state filename does not match document")
		}
		if err := validateDocumentState(old, rel); err != nil {
			return nil, nil, err
		}
		if _, ok := present[rel]; ok {
			continue
		}
		if !old.Retired {
			if _, err := checkManaged(destRoot, old); err != nil {
				return nil, nil, err
			}
			removals, err := ownedChanges(destRoot, old, map[string][]byte{})
			if err != nil {
				return nil, nil, err
			}
			changes = append(changes, removals...)
			next := *old
			next.Outputs = map[string]string{}
			next.Complete = false
			next.Retired = true
			next.RetiredReason = localAbsentReason
			data, err := json.Marshal(next)
			if err != nil {
				return nil, nil, err
			}
			if len(data) > maxMetadata {
				return nil, nil, errors.New("retired conversion state size limit")
			}
			changes = append(changes, mutation{path: path, data: data, mode: 0600})
		}
		retired = append(retired, retiredSource{Path: rel, SourceHash: old.SourceHash, Reason: localAbsentReason})
	}
	sort.Slice(retired, func(i, j int) bool { return retired[i].Path < retired[j].Path })
	return changes, retired, nil
}
