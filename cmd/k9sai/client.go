// SPDX-License-Identifier: Apache-2.0
// Copyright Authors of K9s

package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

const (
	daemonStartTimeout = 20 * time.Second
	healthTimeout      = 2 * time.Second
	logTailLines       = 15
)

var knownTasks = map[string]bool{taskDiagnose: true, taskLogs: true, taskAsk: true, taskCapture: true}

type client struct {
	socket string
	http   *http.Client
}

// event is one SSE frame from the daemon.
type event struct {
	name string
	data json.RawMessage
}

// defaultSocket must match k9sai.server.default_socket in the daemon.
func defaultSocket() string {
	if s := os.Getenv("K9SAI_SOCKET"); s != "" {
		return s
	}
	if rt := os.Getenv("XDG_RUNTIME_DIR"); rt != "" {
		return filepath.Join(rt, "k9sai", "daemon.sock")
	}
	return filepath.Join(stateDir(), "daemon.sock")
}

func stateDir() string {
	home, err := os.UserHomeDir()
	if err != nil {
		home = os.TempDir()
	}
	return filepath.Join(home, ".local", "state", "k9sai")
}

func newClient(socket string) *client {
	if socket == "" {
		socket = defaultSocket()
	}
	dialer := net.Dialer{}
	return &client{
		socket: socket,
		http: &http.Client{Transport: &http.Transport{
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				return dialer.DialContext(ctx, "unix", socket)
			},
		}},
	}
}

func (c *client) health(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, healthTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://k9sai/healthz", http.NoBody)
	if err != nil {
		return err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("daemon health: %s", resp.Status)
	}
	return nil
}

// ensureDaemon starts `k9sai-daemon serve` in its own session when the socket
// is not answering, then waits for it to come up.
func (c *client) ensureDaemon(ctx context.Context) error {
	if c.health(ctx) == nil {
		return nil
	}
	bin := os.Getenv("K9SAI_DAEMON")
	if bin == "" {
		bin = "k9sai-daemon"
	}
	if err := os.MkdirAll(stateDir(), 0o700); err != nil {
		return err
	}
	logPath := filepath.Join(stateDir(), "daemon.log")
	logFile, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	defer logFile.Close()

	// Not tied to ctx: the daemon must outlive this client.
	cmd := exec.CommandContext(context.WithoutCancel(ctx), bin, "serve", "--socket", c.socket) //nolint:gosec // G702: binary chosen by the user's own environment
	cmd.Stdout, cmd.Stderr = logFile, logFile
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("starting %s (install it with `uv tool install ./ai/daemon`): %w", bin, err)
	}
	exited := make(chan error, 1)
	go func() { exited <- cmd.Wait() }()

	deadline := time.Now().Add(daemonStartTimeout)
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case err := <-exited:
			return fmt.Errorf("daemon exited during startup (%w):\n%s", err, tail(logPath, logTailLines))
		case <-time.After(200 * time.Millisecond):
		}
		if c.health(ctx) == nil {
			return nil
		}
	}
	return fmt.Errorf("daemon did not come up within %s:\n%s", daemonStartTimeout, tail(logPath, logTailLines))
}

// stream POSTs the request and calls fn for each SSE frame until `done`.
// Canceling ctx closes the connection, which cancels generation in the daemon.
func (c *client) stream(ctx context.Context, task string, r *request, fn func(event) error) error {
	if !knownTasks[task] {
		return fmt.Errorf("unknown task %q", task)
	}
	body, err := json.Marshal(r)
	if err != nil {
		return err
	}
	// Fixed host, dialed over the unix socket; task is from knownTasks.
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://k9sai/v1/"+task, bytes.NewReader(body)) //nolint:gosec // G704: see above
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(req) //nolint:gosec // G704: unix socket only
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("daemon: %s: %s", resp.Status, bytes.TrimSpace(msg))
	}
	return readSSE(resp.Body, fn)
}

func readSSE(r io.Reader, fn func(event) error) error {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	var ev event
	for sc.Scan() {
		line := sc.Text()
		switch {
		case line == "":
			if ev.name == "done" {
				return nil
			}
			if ev.name != "" {
				if err := fn(ev); err != nil {
					return err
				}
			}
			ev = event{}
		case strings.HasPrefix(line, "event: "):
			ev.name = strings.TrimPrefix(line, "event: ")
		case strings.HasPrefix(line, "data: "):
			ev.data = json.RawMessage(strings.TrimPrefix(line, "data: "))
		}
	}
	if err := sc.Err(); err != nil {
		return err
	}
	return errors.New("daemon closed the stream before finishing")
}

func tail(path string, n int) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	lines := strings.Split(strings.TrimRight(string(b), "\n"), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "\n")
}
