// SPDX-License-Identifier: Apache-2.0
// Copyright Authors of K9s

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// fakeDaemon serves the daemon's HTTP API on a short unix socket path.
func fakeDaemon(t *testing.T, handler http.HandlerFunc) string {
	t.Helper()
	dir, err := os.MkdirTemp("/tmp", "k9sai")
	require.NoError(t, err)
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	sock := filepath.Join(dir, "d.sock")
	var lc net.ListenConfig
	l, err := lc.Listen(context.Background(), "unix", sock)
	require.NoError(t, err)
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"ok":true}`)
	})
	mux.HandleFunc("/v1/", handler)
	srv := &http.Server{Handler: mux, ReadHeaderTimeout: time.Second}
	go func() { _ = srv.Serve(l) }()
	t.Cleanup(func() { _ = srv.Close() })
	return sock
}

func sse(w http.ResponseWriter, frames ...string) {
	for i := 0; i+1 < len(frames); i += 2 {
		_, _ = fmt.Fprintf(w, "event: %s\ndata: %s\n\n", frames[i], frames[i+1])
	}
}

func TestParseArgs(t *testing.T) {
	uu := map[string]struct {
		args []string
		err  string
		want request
	}{
		"diagnose from plugin": {
			args: []string{"diagnose", "--context", "kind", "-n", "shop", "--resource", "deployments", "web"},
			want: request{Context: "kind", Namespace: "shop", Resource: "deployments", Name: "web", Tools: true},
		},
		"empty context from k9s is fine": {
			args: []string{"diagnose", "--context", "", "-n", "shop", "web-1"},
			want: request{Namespace: "shop", Resource: "pods", Name: "web-1", Tools: true},
		},
		"logs forces pods": {
			args: []string{"logs", "--resource", "deployments", "--container", "app", "web-1"},
			want: request{Namespace: "default", Resource: "pods", Name: "web-1", Container: "app", Tools: true},
		},
		"ask keeps the question as one string": {
			args: []string{"ask", "-n", "pay", "pods restarting; rm -rf / $(x)"},
			want: request{Namespace: "pay", Resource: "pods", Question: "pods restarting; rm -rf / $(x)", Tools: true},
		},
		"no-tools":         {args: []string{"diagnose", "--no-tools", "p"}, want: request{Namespace: "default", Resource: "pods", Name: "p"}},
		"missing name":     {args: []string{"diagnose", "-n", "x"}, err: "exactly one resource name"},
		"empty name":       {args: []string{"diagnose", ""}, err: "exactly one resource name"},
		"empty question":   {args: []string{"ask", "  "}, err: "needs a question"},
		"capture needs -o": {args: []string{"capture", "p"}, err: "needs -o"},
		"unknown":          {args: []string{"nope"}, err: "unknown command"},
		"nothing":          {err: "missing command"},
	}
	t.Setenv("KUBECONFIG", "")
	for k := range uu {
		u := uu[k]
		t.Run(k, func(t *testing.T) {
			opts, err := parseArgs(u.args)
			if u.err != "" {
				require.ErrorContains(t, err, u.err)
				return
			}
			require.NoError(t, err)
			assert.Equal(t, u.want, opts.req)
		})
	}
}

func TestReadSSE(t *testing.T) {
	in := "event: meta\ndata: {\"a\":1}\n\nevent: token\ndata: {\"text\":\"hi\"}\n\nevent: done\ndata: {}\n\n"
	var got []string
	require.NoError(t, readSSE(strings.NewReader(in), func(ev event) error {
		got = append(got, ev.name+":"+string(ev.data))
		return nil
	}))
	assert.Equal(t, []string{`meta:{"a":1}`, `token:{"text":"hi"}`}, got)

	err := readSSE(strings.NewReader("event: token\ndata: {}\n\n"), func(event) error { return nil })
	require.ErrorContains(t, err, "before finishing")
}

func TestDiagnoseRendersStream(t *testing.T) {
	var body request
	sock := fakeDaemon(t, func(w http.ResponseWriter, r *http.Request) {
		assert.Equal(t, "/v1/diagnose", r.URL.Path)
		assert.NoError(t, json.NewDecoder(r.Body).Decode(&body))
		sse(w,
			"meta", `{"task":"diagnose","target":"pods/web-1 in namespace shop","backend":"o","model":"m","evidence_lines":12,"dropped":["logs: 3/9 lines"]}`,
			"tool", `{"name":"get_events"}`,
			"token", `{"text":"OOMKilled [E4]"}`,
			"footer", `{"text":"\n⚠️ Cited evidence that does not exist: E99."}`,
			"done", `{}`)
	})
	var out, errOut bytes.Buffer
	code := run(context.Background(), []string{"diagnose", "--socket", sock, "-n", "shop", "--context", "kind", "web-1"}, &out, &errOut)
	require.Equal(t, 0, code, errOut.String())
	s := out.String()
	assert.Contains(t, s, "12 evidence lines")
	assert.Contains(t, s, "dropped: logs: 3/9 lines")
	assert.Contains(t, s, "[read-only tool: get_events]")
	assert.NotContains(t, s, "was a draft") // tool call came before any answer text
	assert.Contains(t, s, "OOMKilled [E4]")
	assert.Contains(t, s, "E99")
	assert.Equal(t, "kind", body.Context)
	assert.Equal(t, "web-1", body.Name)
}

func TestDaemonErrorFailsJSONMode(t *testing.T) {
	sock := fakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		sse(w, "error", `{"message":"ApiException: 403 Forbidden"}`, "done", `{}`)
	})
	var out, errOut bytes.Buffer
	code := run(context.Background(), []string{"diagnose", "--json", "--socket", sock, "p"}, &out, &errOut)
	assert.Equal(t, 1, code)
	assert.Contains(t, errOut.String(), "403 Forbidden")
}

func TestCaptureWritesPrivateFile(t *testing.T) {
	sock := fakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		sse(w, "result", `{"evidence":{"sections":[]}}`, "done", `{}`)
	})
	path := filepath.Join(t.TempDir(), "cap.json")
	var out, errOut bytes.Buffer
	code := run(context.Background(), []string{"capture", "--socket", sock, "-o", path, "web-1"}, &out, &errOut)
	require.Equal(t, 0, code, errOut.String())
	st, err := os.Stat(path)
	require.NoError(t, err)
	assert.Equal(t, os.FileMode(0o600), st.Mode().Perm())
	b, err := os.ReadFile(path)
	require.NoError(t, err)
	assert.JSONEq(t, `{"evidence":{"sections":[]}}`, string(b))
}

func TestHTTPErrorIsReported(t *testing.T) {
	sock := fakeDaemon(t, func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, `{"error":"bad request"}`, http.StatusBadRequest)
	})
	var out, errOut bytes.Buffer
	code := run(context.Background(), []string{"logs", "--no-pager", "--socket", sock, "p"}, &out, &errOut)
	assert.Equal(t, 1, code)
	assert.Contains(t, errOut.String(), "400")
}

// Quitting the pager must cancel the in-flight stream (and so the generation).
func TestQuittingPagerCancelsStream(t *testing.T) {
	var canceled atomic.Bool
	sock := fakeDaemon(t, func(w http.ResponseWriter, r *http.Request) {
		f, _ := w.(http.Flusher)
		for i := 0; ; i++ {
			select {
			case <-r.Context().Done():
				canceled.Store(true)
				return
			case <-time.After(5 * time.Millisecond):
			}
			sse(w, "token", fmt.Sprintf(`{"text":"tok %d "}`, i))
			f.Flush()
		}
	})
	pager := filepath.Join(t.TempDir(), "pager.sh")
	require.NoError(t, os.WriteFile(pager, []byte("#!/bin/sh\nhead -c 200 >/dev/null\n"), 0o700)) //nolint:gosec // G306: test script must be executable
	t.Setenv("K9SAI_PAGER", pager)

	c := newClient(sock)
	opts := &options{task: "diagnose", req: request{Name: "p"}}
	done := make(chan error, 1)
	go func() {
		done <- page(context.Background(), io.Discard, func(ctx context.Context, w io.Writer) error {
			return c.stream(ctx, "diagnose", &opts.req, newRenderer(w, opts).handle)
		})
	}()
	select {
	case err := <-done:
		require.NoError(t, err)
	case <-time.After(10 * time.Second):
		t.Fatal("page did not return after the pager quit")
	}
	assert.Eventually(t, canceled.Load, 2*time.Second, 10*time.Millisecond)
}

func TestStatusWithoutDaemon(t *testing.T) {
	var out bytes.Buffer
	code := run(context.Background(), []string{"status", "--socket", "/tmp/k9sai-none/nope.sock"}, &out, io.Discard)
	assert.Equal(t, 1, code)
	assert.Contains(t, out.String(), "not running")
}

func TestEnsureDaemonReportsStartupFailure(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	bin := filepath.Join(t.TempDir(), "k9sai-daemon")
	script := []byte("#!/bin/sh\necho 'k9sai: config error: no config' >&2\nexit 2\n")
	require.NoError(t, os.WriteFile(bin, script, 0o700)) //nolint:gosec // G306: test script must be executable
	t.Setenv("K9SAI_DAEMON", bin)
	c := newClient("/tmp/k9sai-none/nope.sock")
	err := c.ensureDaemon(context.Background())
	require.ErrorContains(t, err, "config error: no config")
}

func TestDraftDividerWhenToolFollowsText(t *testing.T) {
	var out bytes.Buffer
	r := newRenderer(&out, &options{task: "diagnose"})
	for _, ev := range []event{
		{name: "token", data: []byte(`{"text":"## Summary draft"}`)},
		{name: "tool", data: []byte(`{"name":"get_logs"}`)},
		{name: "tool", data: []byte(`{"name":"get_events"}`)},
		{name: "token", data: []byte(`{"text":"## Summary final"}`)},
	} {
		require.NoError(t, r.handle(ev))
	}
	assert.Equal(t, 1, strings.Count(out.String(), "was a draft"))
}
