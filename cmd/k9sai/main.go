// SPDX-License-Identifier: Apache-2.0
// Copyright Authors of K9s

// Command k9sai is the thin client that k9s plugins call. It talks to the
// k9sai daemon (ai/daemon) over HTTP on a unix socket, starting the daemon on
// demand, and streams the answer into a pager. See ai/DESIGN.md.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"strings"
	"syscall"
)

const usage = `usage: k9sai <command> [flags] <args>

commands:
  diagnose  --resource pods|deployments|statefulsets|daemonsets|replicasets NAME
  logs      [--container C] POD
  ask       QUESTION...
  capture   --resource R NAME -o FILE   (snapshot redacted evidence as an eval fixture)
  status                               (show whether the daemon is running)

common flags: --context C  -n NAMESPACE  --kubeconfig PATH  --no-pager  --socket PATH
`

const (
	taskCapture = "capture"
	evResult    = "result"
	evError     = "error"
)

// request mirrors k9sai.tasks.Request in the daemon.
type request struct {
	Context    string `json:"context"`
	Kubeconfig string `json:"kubeconfig"`
	Namespace  string `json:"namespace"`
	Resource   string `json:"resource,omitempty"`
	Name       string `json:"name,omitempty"`
	Container  string `json:"container,omitempty"`
	Question   string `json:"question,omitempty"`
	Budget     int    `json:"budget,omitempty"`
	Tools      bool   `json:"tools"`
	Thoughts   bool   `json:"thoughts"`
	JSON       bool   `json:"json"`
}

type options struct {
	req     request
	task    string
	socket  string
	noPager bool
	out     string
}

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	code := run(ctx, os.Args[1:], os.Stdout, os.Stderr)
	cancel()
	os.Exit(code)
}

func run(ctx context.Context, args []string, stdout, stderr io.Writer) int {
	opts, err := parseArgs(args)
	if err != nil {
		if !errors.Is(err, flag.ErrHelp) {
			_, _ = fmt.Fprintf(stderr, "k9sai: %v\n\n", err)
		}
		_, _ = fmt.Fprint(stderr, usage)
		return 2
	}
	c := newClient(opts.socket)
	if opts.task == "status" {
		if err := c.health(ctx); err != nil {
			_, _ = fmt.Fprintf(stdout, "daemon not running on %s\n", c.socket)
			return 1
		}
		_, _ = fmt.Fprintf(stdout, "daemon running on %s\n", c.socket)
		return 0
	}
	if err := c.ensureDaemon(ctx); err != nil {
		return fail(stdout, stderr, opts, err)
	}
	if err := execute(ctx, c, opts, stdout); err != nil {
		return fail(stdout, stderr, opts, err)
	}
	return 0
}

// fail reports an error. Plugins run in the foreground and k9s resumes as soon
// as we exit, so on a terminal the error goes through the pager to stay visible.
func fail(stdout, stderr io.Writer, opts *options, err error) int {
	if errors.Is(err, errShown) {
		return 1
	}
	msg := fmt.Sprintf("k9sai: %v\n", err)
	if opts.noPager || !isTerminal(stdout) {
		_, _ = fmt.Fprint(stderr, msg)
		return 1
	}
	_ = page(context.Background(), stdout, func(_ context.Context, w io.Writer) error {
		_, e := io.WriteString(w, msg)
		return e
	})
	return 1
}

func parseArgs(args []string) (*options, error) {
	if len(args) == 0 {
		return nil, errors.New("missing command")
	}
	opts := options{task: args[0], req: request{Namespace: "default", Kubeconfig: os.Getenv("KUBECONFIG")}}
	fs := flag.NewFlagSet(opts.task, flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	fs.StringVar(&opts.req.Context, "context", "", "kube context")
	fs.StringVar(&opts.req.Namespace, "n", opts.req.Namespace, "namespace")
	fs.StringVar(&opts.req.Namespace, "namespace", opts.req.Namespace, "namespace")
	fs.StringVar(&opts.req.Kubeconfig, "kubeconfig", opts.req.Kubeconfig, "kubeconfig path(s)")
	fs.StringVar(&opts.req.Resource, "resource", "pods", "plural resource name ($RESOURCE_NAME)")
	fs.StringVar(&opts.req.Container, "container", "", "container name")
	fs.IntVar(&opts.req.Budget, "budget", 0, "evidence token budget (0 = config default)")
	noTools := fs.Bool("no-tools", false, "disable follow-up tool calls")
	fs.BoolVar(&opts.req.Thoughts, "thoughts", false, "show model reasoning")
	fs.BoolVar(&opts.req.JSON, "json", false, "print the structured result (eval harness)")
	fs.BoolVar(&opts.noPager, "no-pager", false, "write to stdout instead of a pager")
	fs.StringVar(&opts.socket, "socket", "", "daemon socket path")
	fs.StringVar(&opts.out, "o", "", "output file (capture)")
	if err := fs.Parse(args[1:]); err != nil {
		return nil, err
	}
	opts.req.Tools = !*noTools
	rest := fs.Args()

	switch opts.task {
	case "status":
	case "diagnose", "logs", taskCapture:
		if len(rest) != 1 || rest[0] == "" {
			return nil, fmt.Errorf("%s needs exactly one resource name", opts.task)
		}
		opts.req.Name = rest[0]
		if opts.task == "logs" {
			opts.req.Resource = "pods"
		}
		if opts.task == taskCapture && opts.out == "" {
			return nil, errors.New("capture needs -o FILE")
		}
	case "ask":
		opts.req.Question = strings.TrimSpace(strings.Join(rest, " "))
		if opts.req.Question == "" {
			return nil, errors.New("ask needs a question")
		}
	default:
		return nil, fmt.Errorf("unknown command %q", opts.task)
	}
	return &opts, nil
}

func execute(ctx context.Context, c *client, opts *options, stdout io.Writer) error {
	switch {
	case opts.task == taskCapture:
		return capture(ctx, c, opts, stdout)
	case opts.req.JSON:
		return printResult(ctx, c, opts, stdout)
	case opts.noPager || !isTerminal(stdout):
		return c.stream(ctx, opts.task, &opts.req, newRenderer(stdout, opts).handle)
	default:
		return page(ctx, stdout, func(ctx context.Context, w io.Writer) error {
			return c.stream(ctx, opts.task, &opts.req, newRenderer(w, opts).handle)
		})
	}
}

// collectResult streams a task and returns its `result` frame, failing on `error`.
func collectResult(ctx context.Context, c *client, opts *options) (json.RawMessage, error) {
	var result json.RawMessage
	err := c.stream(ctx, opts.task, &opts.req, func(ev event) error {
		switch ev.name {
		case evResult:
			result = ev.data
		case evError:
			return daemonError(ev.data)
		}
		return nil
	})
	if err == nil && result == nil {
		err = fmt.Errorf("daemon returned no %s result", opts.task)
	}
	return result, err
}

func capture(ctx context.Context, c *client, opts *options, stdout io.Writer) error {
	result, err := collectResult(ctx, c, opts)
	if err != nil {
		return err
	}
	if werr := os.WriteFile(opts.out, result, 0o600); werr != nil {
		return werr
	}
	_, err = fmt.Fprintf(stdout, "captured %s/%s to %s\n", opts.req.Resource, opts.req.Name, opts.out)
	return err
}

func printResult(ctx context.Context, c *client, opts *options, stdout io.Writer) error {
	result, err := collectResult(ctx, c, opts)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintf(stdout, "%s\n", result)
	return err
}

func daemonError(data []byte) error {
	var e struct {
		Message string `json:"message"`
	}
	if err := json.Unmarshal(data, &e); err != nil || e.Message == "" {
		return fmt.Errorf("daemon error: %s", data)
	}
	return errors.New(e.Message)
}
