package ocr

import (
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
)

type transportFunc func(*http.Request) (*http.Response, error)

func (f transportFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

type brokenReader struct{}

func (brokenReader) Read([]byte) (int, error) { return 0, io.ErrUnexpectedEOF }
func (brokenReader) Close() error             { return nil }
func TestResponseLimitsAndReadFailures(t *testing.T) {
	for _, kind := range []string{"size", "read", "http-error", "redirect"} {
		t.Run(kind, func(t *testing.T) {
			c := New("http://configured.invalid/ocr", "private-token", "")
			calls := 0
			c.http.Transport = transportFunc(func(r *http.Request) (*http.Response, error) {
				calls++
				if r.Header.Get("Authorization") != "Bearer private-token" {
					t.Fatal("missing configured auth")
				}
				resp := &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"text":"ok"}`)), Request: r}
				switch kind {
				case "size":
					resp.Body = io.NopCloser(strings.NewReader(strings.Repeat("x", (4<<20)+1)))
				case "read":
					resp.Body = brokenReader{}
				case "http-error":
					resp.StatusCode = 500
					resp.Body = io.NopCloser(strings.NewReader("must-not-log-response-secret"))
				case "redirect":
					resp.StatusCode = 302
					resp.Header.Set("Location", "http://other.invalid/exfiltrate")
				}
				return resp, nil
			})
			_, err := c.Recognise([]byte("image"))
			if err == nil {
				t.Fatal("unsafe response accepted")
			}
			if strings.Contains(err.Error(), "must-not-log") {
				t.Fatal("HTTP response secret logged")
			}
			if calls != 1 {
				t.Fatal("followed redirect")
			}
		})
	}
}
func TestOCRSettingsFingerprintAndSuccess(t *testing.T) {
	c := New("http://configured.invalid/ocr", "token", "entity")
	c.http.Transport = transportFunc(func(r *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{"text":"recognised"}`))}, nil
	})
	got, err := c.Recognise([]byte("image"))
	if err != nil || got != "recognised" {
		t.Fatal(got, err)
	}
	if c.Fingerprint() == New("http://configured.invalid/ocr", "different-token", "entity").Fingerprint() {
		t.Fatal("credential config change not reflected")
	}
	c.http.Transport = transportFunc(func(r *http.Request) (*http.Response, error) { return nil, errors.New("transport failure") })
	if _, err := c.Recognise([]byte("image")); err == nil {
		t.Fatal("transport failure ignored")
	}
}
