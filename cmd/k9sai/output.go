// SPDX-License-Identifier: Apache-2.0
// Copyright Authors of K9s

package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"syscall"
)

const (
	dim   = "\x1b[2m"
	bold  = "\x1b[1m"
	red   = "\x1b[31m"
	reset = "\x1b[0m"
)

type renderer struct {
	w    io.Writer
	opts *options
}

func newRenderer(w io.Writer, opts *options) *renderer {
	return &renderer{w: w, opts: opts}
}

func (r *renderer) handle(ev event) error {
	var d map[string]any
	if err := json.Unmarshal(ev.data, &d); err != nil {
		return fmt.Errorf("bad %s frame: %w", ev.name, err)
	}
	str := func(k string) string { s, _ := d[k].(string); return s }
	var err error
	switch ev.name {
	case "meta":
		err = r.header(d)
	case "token":
		_, err = io.WriteString(r.w, str("text"))
	case "thought":
		_, err = fmt.Fprint(r.w, dim+str("text")+reset)
	case "tool":
		_, err = fmt.Fprintf(r.w, "\n%s[read-only tool: %s]%s\n", dim, str("name"), reset)
	case "footer":
		_, err = io.WriteString(r.w, str("text"))
	case evError:
		_, err = fmt.Fprintf(r.w, "\n%sk9sai error: %s%s\n", red, str("message"), reset)
	case evResult:
		if cmd := str("command"); r.opts.task == "ask" {
			err = r.askResult(cmd)
		}
	}
	return err
}

func (r *renderer) header(d map[string]any) error {
	line := fmt.Sprintf("%sk9sai %s · %s · %s (%s)", bold, d["task"], d["target"], d["backend"], d["model"])
	if n, ok := d["evidence_lines"].(float64); ok {
		line += fmt.Sprintf(" · %d evidence lines", int(n))
	}
	if _, err := fmt.Fprintf(r.w, "%s%s\n", line, reset); err != nil {
		return err
	}
	if dropped, ok := d["dropped"].([]any); ok && len(dropped) > 0 {
		parts := make([]string, 0, len(dropped))
		for _, x := range dropped {
			parts = append(parts, fmt.Sprint(x))
		}
		if _, err := fmt.Fprintf(r.w, "%sover budget, dropped: %s%s\n", dim, strings.Join(parts, "; "), reset); err != nil {
			return err
		}
	}
	_, err := fmt.Fprintf(r.w, "%s(answers are model-generated; check the cited [E#] evidence · q to return to k9s)%s\n\n", dim, reset)
	return err
}

func (r *renderer) askResult(cmd string) error {
	if cmd == "" {
		_, err := fmt.Fprintf(r.w, "\n%sno command found in the answer%s\n", dim, reset)
		return err
	}
	if err := copyToClipboard(cmd); err != nil {
		_, e := fmt.Fprintf(r.w, "\n%sclipboard unavailable (%v); type it yourself: %s%s\n", dim, err, cmd, reset)
		return e
	}
	_, err := fmt.Fprintf(r.w, "\n%scopied to clipboard:%s %s\n", bold, reset, cmd)
	return err
}

func copyToClipboard(s string) error {
	var candidates [][]string
	switch runtime.GOOS {
	case "darwin":
		candidates = [][]string{{"pbcopy"}}
	default:
		candidates = [][]string{{"wl-copy"}, {"xclip", "-selection", "clipboard"}, {"xsel", "--clipboard", "--input"}}
	}
	for _, c := range candidates {
		if _, err := exec.LookPath(c[0]); err != nil {
			continue
		}
		cmd := exec.CommandContext(context.Background(), c[0], c[1:]...)
		cmd.Stdin = strings.NewReader(s)
		return cmd.Run()
	}
	return errors.New("no clipboard tool found")
}

func isTerminal(w io.Writer) bool {
	f, ok := w.(*os.File)
	if !ok {
		return false
	}
	st, err := f.Stat()
	return err == nil && st.Mode()&os.ModeCharDevice != 0
}

// errShown marks an error that was already displayed in the pager.
var errShown = errors.New("error shown in pager")

// page runs fill with the pager's stdin as its writer. Quitting the pager cancels
// the context handed to fill, which closes the stream and aborts generation in
// the daemon.
func page(ctx context.Context, stdout io.Writer, fill func(context.Context, io.Writer) error) error {
	pager := os.Getenv("K9SAI_PAGER")
	if pager == "" {
		pager = "less"
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	// -R: keep colors. The user's $LESS is overridden so -F (quit if one screen)
	// cannot make the answer vanish the moment k9s resumes.
	cmd := exec.CommandContext(context.WithoutCancel(ctx), pager, "-R") //nolint:gosec // G702: pager chosen by the user's own environment, like $PAGER

	cmd.Env = append(os.Environ(), "LESS=")
	cmd.Stdout, cmd.Stderr = stdout, os.Stderr
	in, err := cmd.StdinPipe()
	if err != nil {
		return err
	}
	if err := cmd.Start(); err != nil {
		return fill(ctx, stdout) // no pager available: stream straight to the terminal
	}
	go func() {
		_ = cmd.Wait()
		cancel()
	}()

	fillErr := fill(ctx, in)
	quit := ctx.Err() != nil || errors.Is(fillErr, syscall.EPIPE) || errors.Is(fillErr, os.ErrClosed)
	if fillErr != nil && !quit {
		_, _ = fmt.Fprintf(in, "\n%sk9sai: %v%s\n", red, fillErr, reset)
		fillErr = errShown
	}
	_ = in.Close()
	<-ctx.Done() // wait for the user to quit the pager
	if quit {
		return nil
	}
	return fillErr
}
